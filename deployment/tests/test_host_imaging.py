from __future__ import annotations

import hashlib
import json
import lzma
import os
from pathlib import Path
import subprocess

import jsonschema
import pytest
import yaml

from iii_deployment.contracts import ContractRegistry
from iii_deployment.host_imaging import (
    _partition_one,
    _write_raw_image,
    BootstrapInputError,
    DeviceChangedError,
    ImageVerificationError,
    ImagingError,
    TargetProofError,
    UnsafeDeviceError,
    apply_image_plan,
    build_image_plan,
    inspect_devices,
    inspect_image,
    load_bootstrap_input,
    load_contract,
    render_nocloud_seed,
    select_device,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "deployment" / "schemas" / "v1"
REGISTRY = ContractRegistry(SCHEMAS)
SOURCE = ROOT / "deployment" / "provisioning" / "ubuntu-raspi-image.json"
PROFILE = ROOT / "deployment" / "provisioning" / "cloud-init-profile.json"


def _bootstrap(path: Path, *, wifi: bool = True) -> Path:
    value = {
        "schema": "iii.cloud-init-bootstrap-input/v1",
        "hostname": "iii",
        "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureKey operator@test",
        "bootstrap_credential": "a_secure_one_time_bootstrap_token_1234",
        "network": {
            "ethernet_dhcp4": True,
            "wifi": (
                [{"ssid": "field-net", "password": "not-a-real-secret", "hidden": True}]
                if wifi
                else []
            ),
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _image_fixture(
    root: Path, raw: bytes = b"III raw image fixture" * 64
) -> tuple[Path, Path]:
    image = root / "ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz"
    image.write_bytes(lzma.compress(raw))
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    source["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    source["minimum_target_bytes"] = 8589934592
    source_path = root / "source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    return image, source_path


def _lsblk(
    *, target_size: int = 16 * 1024**3, target_updates: dict | None = None
) -> dict:
    target = {
        "name": "sdz",
        "kname": "sdz",
        "path": "/dev/sdz",
        "type": "disk",
        "size": target_size,
        "model": "TEST READER",
        "serial": "SERIAL-1234",
        "tran": "usb",
        "rm": True,
        "ro": False,
        "mountpoints": [None],
        "stable_path": "/dev/disk/by-id/usb-TEST_SERIAL-1234",
        "children": [
            {
                "name": "sdz1",
                "kname": "sdz1",
                "path": "/dev/sdz1",
                "type": "part",
                "size": target_size - 1,
                "pkname": "sdz",
                "mountpoints": [None],
            }
        ],
    }
    target.update(target_updates or {})
    return {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "kname": "nvme0n1",
                "path": "/dev/nvme0n1",
                "type": "disk",
                "size": 100 * 1024**3,
                "model": "SYSTEM",
                "serial": "SYSTEM-1",
                "tran": "nvme",
                "rm": False,
                "ro": False,
                "mountpoints": [None],
                "children": [
                    {
                        "name": "nvme0n1p2",
                        "kname": "nvme0n1p2",
                        "path": "/dev/nvme0n1p2",
                        "type": "part",
                        "size": 99 * 1024**3,
                        "pkname": "nvme0n1",
                        "mountpoints": ["/"],
                    }
                ],
            },
            target,
        ]
    }


def test_tracked_source_and_cloud_init_profiles_validate_and_preserve_upstream_layout() -> (
    None
):
    source = load_contract(
        SOURCE, schema_name="host-image-source", registry=REGISTRY, label="source"
    )
    profile = load_contract(
        PROFILE, schema_name="cloud-init-profile", registry=REGISTRY, label="profile"
    )
    assert (
        source["sha256"]
        == "790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37"
    )
    assert source["partition_contract"] == {
        "strategy": "upstream-image",
        "boot_partition": "upstream-supported-raspberry-pi",
        "root_filesystem": "upstream-auto-expanded-ext4",
        "custom_partitions": False,
        "lvm": False,
        "encryption": False,
        "ab_root": False,
        "persistent_paths_share_root": ["/opt/iii", "/var/lib/iii", "/var/log/iii"],
    }
    assert profile["application_installation"] is False
    assert profile["wifi_interface"] == "wlan0"
    assert profile["ethernet_recovery"]["match_name"] == "enx*"
    assert profile["px4_ethernet"] == {
        "required": True,
        "match_name": "eth0",
        "address": "10.41.10.1/24",
        "peer_address": "10.41.10.2",
        "mavlink_udp_port": 14540,
        "uxrce_dds_udp_port": 8888,
    }
    assert profile["sanitization_contract"]["failure_blocks_commissioning"] is True


def test_owner_only_bootstrap_input_renders_recovery_ethernet_and_diagnosable_seed(
    tmp_path: Path,
) -> None:
    bootstrap_path = _bootstrap(tmp_path / "bootstrap.json")
    bootstrap = load_bootstrap_input(bootstrap_path, REGISTRY)
    profile = load_contract(
        PROFILE, schema_name="cloud-init-profile", registry=REGISTRY, label="profile"
    )
    seed = render_nocloud_seed(profile=profile, bootstrap=bootstrap)
    user_data = seed["files"]["user-data"].decode()
    network = seed["files"]["network-config"].decode()
    all_seed = b"".join(seed["files"].values())
    assert b"a_secure_one_time_bootstrap_token_1234" not in all_seed
    assert b"BEGIN PRIVATE KEY" not in all_seed
    assert "ssh_pwauth" in user_data and "bootstrap-cloud-init.log" in user_data
    assert "operator-usb-ethernet" in network and '"dhcp4": true' in network
    parsed_network = yaml.safe_load(network)
    assert parsed_network["ethernets"]["operator-usb-ethernet"] == {
        "match": {"name": "enx*"},
        "dhcp4": True,
        "optional": True,
    }
    assert parsed_network["ethernets"]["px4-ethernet"] == {
        "match": {"name": "eth0"},
        "addresses": ["10.41.10.1/24"],
        "dhcp4": False,
        "link-local": [],
        "optional": True,
    }
    assert "field-net" in network and "not-a-real-secret" in network
    assert "wlan0" in network
    assert seed["contains_network_secret"] is True
    parsed_user_data = yaml.safe_load(user_data.removeprefix("#cloud-config\n"))
    assert parsed_user_data["ssh_pwauth"] is False
    assert parsed_user_data["disable_root"] is True
    assert parsed_user_data["package_update"] is False
    assert "output" not in parsed_user_data
    rendered_commands = json.dumps(parsed_user_data["runcmd"], sort_keys=True)
    assert (
        "install -D -o root -g adm -m 0640 /var/log/cloud-init-output.log "
        "/var/log/iii/bootstrap-cloud-init.log"
    ) in rendered_commands


def test_ethernet_only_and_multiple_wifi_bootstrap_profiles(tmp_path: Path) -> None:
    profile = load_contract(
        PROFILE, schema_name="cloud-init-profile", registry=REGISTRY, label="profile"
    )
    ethernet = render_nocloud_seed(
        profile=profile,
        bootstrap=load_bootstrap_input(
            _bootstrap(tmp_path / "ethernet.json", wifi=False), REGISTRY
        ),
    )
    ethernet_network = yaml.safe_load(ethernet["files"]["network-config"])
    assert "wifis" not in ethernet_network
    assert ethernet["contains_network_secret"] is False

    path = _bootstrap(tmp_path / "multiple.json", wifi=False)
    value = json.loads(path.read_text())
    value["network"]["wifi"] = [
        {"ssid": "field-a", "password": "field-secret-a"},
        {"ssid": "field-b", "password": "field-secret-b", "hidden": True},
    ]
    path.write_text(json.dumps(value))
    multiple = render_nocloud_seed(
        profile=profile, bootstrap=load_bootstrap_input(path, REGISTRY)
    )
    network = yaml.safe_load(multiple["files"]["network-config"])
    assert set(network["wifis"]["wlan0"]["access-points"]) == {
        "field-a",
        "field-b",
    }
    assert network["ethernets"]["operator-usb-ethernet"]["dhcp4"] is True
    assert network["ethernets"]["px4-ethernet"]["addresses"] == ["10.41.10.1/24"]
    assert all(
        "field-secret" not in json.dumps(row) for row in multiple["file_evidence"]
    )


def test_bootstrap_duplicate_wifi_ssids_fail_closed(tmp_path: Path) -> None:
    path = _bootstrap(tmp_path / "duplicates.json", wifi=False)
    value = json.loads(path.read_text())
    value["network"]["wifi"] = [
        {"ssid": "same", "password": "first-secret"},
        {"ssid": "same", "password": "second-secret"},
    ]
    path.write_text(json.dumps(value))
    with pytest.raises(BootstrapInputError, match="duplicate"):
        load_bootstrap_input(path, REGISTRY)


def test_bootstrap_input_permissions_and_private_material_fail_closed(
    tmp_path: Path,
) -> None:
    path = _bootstrap(tmp_path / "bootstrap.json")
    path.chmod(0o644)
    with pytest.raises(BootstrapInputError, match="owner-only"):
        load_bootstrap_input(path, REGISTRY)
    path.chmod(0o600)
    value = json.loads(path.read_text())
    value["ssh_public_key"] = "-----BEGIN OPENSSH PRIVATE KEY-----"
    path.write_text(json.dumps(value))
    with pytest.raises(BootstrapInputError):
        load_bootstrap_input(path, REGISTRY)


def test_bootstrap_input_inside_git_must_be_ignored_and_credential_has_entropy(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    tracked = _bootstrap(tmp_path / "tracked.json")
    subprocess.run(["git", "add", "tracked.json"], cwd=tmp_path, check=True)
    with pytest.raises(ImagingError, match="ignore rule"):
        load_bootstrap_input(tracked, REGISTRY)
    ignored = _bootstrap(tmp_path / "ignored.json")
    (tmp_path / ".gitignore").write_text("ignored.json\n")
    assert load_bootstrap_input(ignored, REGISTRY)["hostname"] == "iii"
    value = json.loads(ignored.read_text())
    value["bootstrap_credential"] = "a" * 32
    ignored.write_text(json.dumps(value))
    with pytest.raises(BootstrapInputError, match="distinct characters"):
        load_bootstrap_input(ignored, REGISTRY)


def test_pinned_image_is_fully_decompressed_and_hash_verified_before_use(
    tmp_path: Path,
) -> None:
    image, source_path = _image_fixture(tmp_path)
    source = load_contract(
        source_path, schema_name="host-image-source", registry=REGISTRY, label="source"
    )
    evidence = inspect_image(image, source)
    assert evidence["verified"] is True
    assert evidence["raw_bytes"] > 0
    image.write_bytes(image.read_bytes() + b"tamper")
    with pytest.raises(ImageVerificationError, match="SHA-256"):
        inspect_image(image, source)


def test_raw_writer_streams_and_reads_back_the_complete_decompressed_image(
    tmp_path: Path,
) -> None:
    raw = os.urandom(2 * 1024 * 1024 + 17)
    image, source_path = _image_fixture(tmp_path, raw=raw)
    source = load_contract(
        source_path,
        schema_name="host-image-source",
        registry=REGISTRY,
        label="source",
    )
    expected = inspect_image(image, source)
    target = tmp_path / "block-device-fixture"
    target.touch()
    evidence = _write_raw_image(image, target, expected, allow_regular_file=True)
    assert target.read_bytes() == raw
    assert evidence == {
        "bytes": len(raw),
        "stream_sha256": hashlib.sha256(raw).hexdigest(),
        "readback_sha256": hashlib.sha256(raw).hexdigest(),
        "verified": True,
    }


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"rm": False}, "not-removable"),
        ({"ro": True}, "read-only"),
        ({"size": 1024}, "smaller-than"),
        ({"mountpoints": ["/media/operator/card"]}, "mounted-or-in-use"),
    ],
)
def test_device_inspection_rejects_unsafe_media_classes(
    updates: dict, reason: str
) -> None:
    devices = inspect_devices(
        minimum_bytes=8 * 1024**3,
        lsblk=_lsblk(target_updates=updates),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    target = next(row for row in devices if row["kernel_path"] == "/dev/sdz")
    assert target["eligible"] is False
    assert any(reason in item for item in target["rejection_reasons"])
    system = next(row for row in devices if row["kernel_path"] == "/dev/nvme0n1")
    assert system["backs_running_system"] is True
    assert "backs-running-system" in system["rejection_reasons"]


def test_device_selection_requires_exact_stable_path_and_reports_identity() -> None:
    devices = inspect_devices(
        minimum_bytes=8 * 1024**3,
        lsblk=_lsblk(),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    selected = select_device(devices, "/dev/disk/by-id/usb-TEST_SERIAL-1234")
    assert selected["eligible"] is True
    assert selected["model"] == "TEST READER"
    with pytest.raises(UnsafeDeviceError, match="/dev/disk/by-id"):
        select_device(devices, "/dev/sdz")


def test_partition_discovery_retries_transient_reader_reenumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lsblk_attempts = 0

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        nonlocal lsblk_attempts
        if argv[0] != "lsblk":
            return subprocess.CompletedProcess(argv, 0, "", "")
        lsblk_attempts += 1
        if lsblk_attempts == 1:
            return subprocess.CompletedProcess(argv, 1, "", "device temporarily absent")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "blockdevices": [
                        {
                            "path": "/dev/sdd",
                            "type": "disk",
                            "fstype": None,
                            "children": [
                                {
                                    "path": "/dev/sdd1",
                                    "type": "part",
                                    "fstype": "vfat",
                                }
                            ],
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr("iii_deployment.host_imaging.subprocess.run", run)

    assert _partition_one(
        "/dev/disk/by-id/usb-Kingston_Multi-Reader_-3",
        partition_number_reader=lambda path: 1 if path == "/dev/sdd1" else None,
    ) == Path("/dev/sdd1")
    assert lsblk_attempts == 2


def test_string_flags_and_nested_mapper_ancestry_fail_closed() -> None:
    topology = _lsblk(target_updates={"rm": "0", "ro": "0"})
    topology["blockdevices"][1]["children"][0]["children"] = [
        {
            "name": "dm-9",
            "kname": "dm-9",
            "path": "/dev/dm-9",
            "type": "crypt",
            "size": 9 * 1024**3,
            "pkname": "sdz1",
            "mountpoints": [None],
        }
    ]
    devices = inspect_devices(
        minimum_bytes=8 * 1024**3,
        lsblk=topology,
        running_sources=["/dev/dm-9"],
        by_id_root=Path("/missing"),
    )
    target = next(row for row in devices if row["kernel_path"] == "/dev/sdz")
    assert target["removable"] is False
    assert target["backs_running_system"] is True
    assert "unresolved-device-mapper-or-holder" in target["rejection_reasons"]


def _build_plan(
    tmp_path: Path, *, accept_data_loss: bool = True
) -> tuple[dict, Path, Path]:
    image, source = _image_fixture(tmp_path)
    bootstrap = _bootstrap(tmp_path / "bootstrap.json")
    (tmp_path / "evidence").mkdir(exist_ok=True)
    (tmp_path / "evidence").chmod(0o700)
    plan = build_image_plan(
        operation_id="iii-host-image-test",
        image_path=image,
        source_path=source,
        profile_path=PROFILE,
        bootstrap_input_path=bootstrap,
        device_path="/dev/disk/by-id/usb-TEST_SERIAL-1234",
        schema_root=SCHEMAS,
        evidence_directory=tmp_path / "evidence",
        backup_record=None,
        accept_data_loss=accept_data_loss,
        lsblk=_lsblk(),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    return plan, image, bootstrap


def test_plan_requires_separate_backup_or_data_loss_authority_and_never_retains_secrets(
    tmp_path: Path,
) -> None:
    with pytest.raises(ImagingError, match="backup record or explicit"):
        _build_plan(tmp_path, accept_data_loss=False)
    plan, _, _ = _build_plan(tmp_path)
    serialized = json.dumps(plan, sort_keys=True)
    assert "not-a-real-secret" not in serialized
    assert "a_secure_one_time_bootstrap_token" not in serialized
    assert plan["destructive_authority"]["required_typed_proof"].startswith(
        "ERASE AND ACCEPT DATA LOSS /dev/disk/by-id/"
    )
    assert plan["partition_contract"]["custom_partitions"] is False


def test_plan_accepts_only_verified_host_backup_or_salvage_evidence(
    tmp_path: Path,
) -> None:
    image, source = _image_fixture(tmp_path)
    bootstrap = _bootstrap(tmp_path / "bootstrap.json")
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence_directory.chmod(0o700)
    backup = tmp_path / "backup.json"
    backup_value = {
        "schema": "iii.host-backup-receipt/v1",
        "backup_id": "a" * 64,
        "receipt_id": "0" * 64,
        "verified": True,
        "external_verified": True,
        "fresh": True,
        "target_state_hash": "b" * 64,
        "state_marker": "c" * 64,
        "archive_sha256": "d" * 64,
    }
    backup_value["receipt_id"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in backup_value.items() if key != "receipt_id"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    backup.write_text(json.dumps(backup_value))
    plan = build_image_plan(
        operation_id="iii-host-image-backup",
        image_path=image,
        source_path=source,
        profile_path=PROFILE,
        bootstrap_input_path=bootstrap,
        device_path="/dev/disk/by-id/usb-TEST_SERIAL-1234",
        schema_root=SCHEMAS,
        evidence_directory=evidence_directory,
        backup_record=backup,
        accept_data_loss=False,
        lsblk=_lsblk(),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    assert plan["destructive_authority"]["accepted_data_loss"] is False
    assert (
        plan["destructive_authority"]["backup_record"]["sha256"]
        == hashlib.sha256(backup.read_bytes()).hexdigest()
    )
    backup.write_text(
        json.dumps({"schema": "iii.record-archive-receipt/v1", "verified": True})
    )
    with pytest.raises(ImagingError, match="not a verified host backup"):
        build_image_plan(
            operation_id="iii-host-image-bad-backup",
            image_path=image,
            source_path=source,
            profile_path=PROFILE,
            bootstrap_input_path=bootstrap,
            device_path="/dev/disk/by-id/usb-TEST_SERIAL-1234",
            schema_root=SCHEMAS,
            evidence_directory=evidence_directory,
            backup_record=backup,
            accept_data_loss=False,
            lsblk=_lsblk(),
            running_sources=["/dev/nvme0n1p2"],
            by_id_root=Path("/missing"),
        )


def test_verified_salvage_authorizes_reimage_but_retains_recommissioning_boundary(
    tmp_path: Path,
) -> None:
    image, source = _image_fixture(tmp_path)
    bootstrap = _bootstrap(tmp_path / "bootstrap.json")
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence_directory.chmod(0o700)
    salvage = {
        "schema": "iii.host-salvage-record/v1",
        "salvage_id": "0" * 64,
        "backup_id": "a" * 64,
        "outcome": "verified",
        "verified": True,
        "recorded_at": "2026-08-27T12:00:00Z",
        "source_device": {
            "stable_path": "/dev/disk/by-id/usb-source",
            "resolved_path": "/dev/sdz",
            "fingerprint": "b" * 64,
            "root_partition": "/dev/sdz2",
            "layout": "ubuntu-raspi-single-ext4-root",
        },
        "filesystem": {
            "type": "ext4",
            "mount_enforcement": "kernel-read-only-ro-noload-nodev-nosuid-noexec",
            "transaction_consistency": "e2fsck-read-only-clean",
            "source_modified": False,
        },
        "recoverable_domains": ["configuration"],
        "omissions": ["credentials"],
        "target_state_hash": "c" * 64,
        "archive_sha256": "d" * 64,
        "credentials_recovered": False,
        "recommissioning_required": True,
        "operator_notice": "Fresh credentials, a clean reimage, and full recommissioning remain mandatory; this salvage is not bootable media.",
    }
    salvage["salvage_id"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in salvage.items() if key != "salvage_id"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    salvage_path = tmp_path / "salvage.json"
    salvage_path.write_text(json.dumps(salvage), encoding="utf-8")
    plan = build_image_plan(
        operation_id="iii-host-image-salvage",
        image_path=image,
        source_path=source,
        profile_path=PROFILE,
        bootstrap_input_path=bootstrap,
        device_path="/dev/disk/by-id/usb-TEST_SERIAL-1234",
        schema_root=SCHEMAS,
        evidence_directory=evidence_directory,
        backup_record=salvage_path,
        accept_data_loss=False,
        lsblk=_lsblk(),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    assert plan["destructive_authority"]["accepted_data_loss"] is False
    assert salvage["credentials_recovered"] is False
    assert salvage["recommissioning_required"] is True
    salvage["credentials_recovered"] = True
    salvage_path.write_text(json.dumps(salvage), encoding="utf-8")
    with pytest.raises(ImagingError, match="incomplete or unsafe"):
        build_image_plan(
            operation_id="iii-host-image-salvage-bad",
            image_path=image,
            source_path=source,
            profile_path=PROFILE,
            bootstrap_input_path=bootstrap,
            device_path="/dev/disk/by-id/usb-TEST_SERIAL-1234",
            schema_root=SCHEMAS,
            evidence_directory=evidence_directory,
            backup_record=salvage_path,
            accept_data_loss=False,
            lsblk=_lsblk(),
            running_sources=["/dev/nvme0n1p2"],
            by_id_root=Path("/missing"),
        )


def test_apply_reauthenticates_device_and_refuses_wrong_typed_proof_before_write(
    tmp_path: Path,
) -> None:
    plan, _, _ = _build_plan(tmp_path)
    inspector = lambda **_kwargs: inspect_devices(
        minimum_bytes=8 * 1024**3,
        lsblk=_lsblk(),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    with pytest.raises(TargetProofError):
        apply_image_plan(
            plan,
            schema_root=SCHEMAS,
            proof_reader=lambda _prompt: "wrong",
            device_inspector=inspector,
        )
    changed = _lsblk(target_updates={"serial": "REPLACED"})
    changed_inspector = lambda **_kwargs: inspect_devices(
        minimum_bytes=8 * 1024**3,
        lsblk=changed,
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )
    with pytest.raises(DeviceChangedError, match="identity changed"):
        apply_image_plan(
            plan,
            schema_root=SCHEMAS,
            proof_reader=lambda _prompt: plan["destructive_authority"][
                "required_typed_proof"
            ],
            device_inspector=changed_inspector,
        )


def test_successful_apply_requires_exact_write_seed_flush_and_emits_valid_record(
    tmp_path: Path,
) -> None:
    plan, _, _ = _build_plan(tmp_path)
    inspector = lambda **_kwargs: inspect_devices(
        minimum_bytes=8 * 1024**3,
        lsblk=_lsblk(),
        running_sources=["/dev/nvme0n1p2"],
        by_id_root=Path("/missing"),
    )

    def writer(_image: Path, _target: Path, expected: dict) -> dict:
        return {
            "bytes": expected["raw_bytes"],
            "stream_sha256": expected["raw_sha256"],
            "readback_sha256": expected["raw_sha256"],
            "verified": True,
        }

    observed_targets: list[tuple[str, str]] = []

    def seed_installer(target: str, seed: dict) -> list[dict]:
        observed_targets.append(("seed", target))
        return seed["file_evidence"]

    def ejector(target: str) -> dict:
        observed_targets.append(("eject", target))
        return {
            "fsync": True,
            "block_buffers": True,
            "eject_requested": True,
            "method": "fixture-eject",
        }

    record = apply_image_plan(
        plan,
        schema_root=SCHEMAS,
        proof_reader=lambda _prompt: plan["destructive_authority"][
            "required_typed_proof"
        ],
        device_inspector=inspector,
        image_writer=writer,
        seed_installer=seed_installer,
        ejector=ejector,
    )
    schema = json.loads((SCHEMAS / "host-image-record.schema.json").read_text())
    jsonschema.Draft7Validator(schema).validate(
        {key: value for key, value in record.items() if key != "evidence_path"}
    )
    assert Path(record["evidence_path"]).is_file()
    assert record["write"]["verified"] is True
    assert record["seed"]["verified"] is True
    assert record["destructive_authority"]["accepted_data_loss"] is True
    assert observed_targets == [
        ("seed", "/dev/disk/by-id/usb-TEST_SERIAL-1234"),
        ("eject", "/dev/disk/by-id/usb-TEST_SERIAL-1234"),
    ]


def test_mutating_apply_refuses_changed_bootstrap_input(tmp_path: Path) -> None:
    plan, _, bootstrap = _build_plan(tmp_path)
    value = json.loads(bootstrap.read_text())
    value["hostname"] = "iii-replaced"
    bootstrap.write_text(json.dumps(value))
    with pytest.raises(DeviceChangedError, match="bootstrap input changed"):
        apply_image_plan(plan, schema_root=SCHEMAS)
