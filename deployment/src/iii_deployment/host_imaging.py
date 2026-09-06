"""Fail-closed Raspberry Pi media imaging and NoCloud seed generation.

The module deliberately separates inspection/planning from the destructive
write.  The retained plan contains content and device fingerprints; apply
re-authenticates all of them immediately before opening the block device.
"""

from __future__ import annotations

from datetime import datetime, timezone
import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractError, ContractRegistry, content_identity


IMAGE_PLAN_SCHEMA = "iii.host-image-plan/v1"
IMAGE_RECORD_SCHEMA = "iii.host-image-record/v1"
BOOTSTRAP_INPUT_SCHEMA = "iii.cloud-init-bootstrap-input/v1"
CHUNK_BYTES = 4 * 1024 * 1024
PARTITION_DISCOVERY_ATTEMPTS = 20
PARTITION_DISCOVERY_DELAY_SECONDS = 0.25


class ImagingError(RuntimeError):
    code = "III_HOST_IMAGE_ERROR"


class ImageVerificationError(ImagingError):
    code = "III_HOST_IMAGE_VERIFICATION_FAILED"


class UnsafeDeviceError(ImagingError):
    code = "III_HOST_IMAGE_DEVICE_UNSAFE"


class DeviceChangedError(ImagingError):
    code = "III_HOST_IMAGE_DEVICE_CHANGED"


class TargetProofError(ImagingError):
    code = "III_HOST_IMAGE_TARGET_PROOF_FAILED"


class BootstrapInputError(ImagingError):
    code = "III_HOST_IMAGE_BOOTSTRAP_INPUT_INVALID"


def _authorized_local_uids() -> set[int]:
    values = {os.geteuid()} if hasattr(os, "geteuid") else set()
    sudo_uid = os.environ.get("SUDO_UID")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and sudo_uid:
        try:
            parsed = int(sudo_uid)
        except ValueError as exc:
            raise ImagingError("SUDO_UID is not a valid local user identity") from exc
        if parsed < 0:
            raise ImagingError("SUDO_UID is not a valid local user identity")
        values.add(parsed)
    return values


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ImagingError(f"{label} must be a real regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImagingError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImagingError(f"{label} must contain one JSON object")
    return value


def _require_non_repository_or_ignored(path: Path, *, label: str) -> None:
    repository = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if repository.returncode != 0:
        return
    root = Path(repository.stdout.strip()).resolve()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError as exc:
        raise ImagingError(f"{label} repository containment is inconsistent") from exc
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(relative)],
        check=False,
        capture_output=True,
        text=True,
    )
    if ignored.returncode == 0:
        return
    if ignored.returncode == 1:
        raise ImagingError(
            f"{label} inside a Git worktree must be covered by an ignore rule"
        )
    raise ImagingError(f"cannot verify Git-ignore status for {label}")


def load_contract(
    path: Path,
    *,
    schema_name: str,
    registry: ContractRegistry,
    label: str,
) -> dict[str, Any]:
    value = _load_object(path, label=label)
    registry.validate(schema_name, value)
    return value


def load_bootstrap_input(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapInputError("bootstrap input must be a real regular file")
    metadata = path.stat()
    if hasattr(os, "geteuid") and metadata.st_uid not in _authorized_local_uids():
        raise BootstrapInputError(
            "bootstrap input must be owned by the current or invoking sudo user"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BootstrapInputError(
            "bootstrap input permissions must be owner-only (0600 or stricter)"
        )
    _require_non_repository_or_ignored(path, label="bootstrap input")
    try:
        value = _load_object(path, label="bootstrap input")
        registry.validate("cloud-init-bootstrap-input", value)
    except Exception as exc:
        if isinstance(exc, BootstrapInputError):
            raise
        raise BootstrapInputError(str(exc)) from exc
    serialized = json.dumps(value, sort_keys=True)
    forbidden = (
        "BEGIN PRIVATE KEY",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
    )
    if any(marker in serialized for marker in forbidden):
        raise BootstrapInputError(
            "private key material is forbidden in bootstrap input"
        )
    if len(set(str(value["bootstrap_credential"]))) < 16:
        raise BootstrapInputError(
            "bootstrap credential must contain at least 16 distinct characters"
        )
    ssids = [str(row["ssid"]) for row in value["network"]["wifi"]]
    if len(ssids) != len(set(ssids)):
        raise BootstrapInputError("bootstrap input contains duplicate Wi-Fi SSIDs")
    return value


def _yaml_document(value: Mapping[str, Any], *, cloud_config: bool = False) -> bytes:
    # JSON is a strict YAML 1.2 subset and avoids an independent renderer whose
    # quoting behavior could expose or corrupt SSIDs and passphrases.
    prefix = "#cloud-config\n" if cloud_config else ""
    return (prefix + json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def render_nocloud_seed(
    *,
    profile: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
) -> dict[str, Any]:
    if profile.get("schema") != "iii.cloud-init-profile/v1":
        raise BootstrapInputError("unsupported cloud-init profile")
    if bootstrap.get("schema") != BOOTSTRAP_INPUT_SCHEMA:
        raise BootstrapInputError("unsupported bootstrap input")

    hostname = str(bootstrap["hostname"])
    if hostname != "iii":
        raise BootstrapInputError(
            "aircraft bootstrap hostname must be iii for iii.local mDNS"
        )
    instance_material = {
        "profile_id": profile["profile_id"],
        "hostname": hostname,
        "ssh_public_key": bootstrap["ssh_public_key"],
        "network": bootstrap["network"],
    }
    instance_id = "iii-" + content_identity(instance_material)[:24]
    credential_hash = hashlib.sha256(
        str(bootstrap["bootstrap_credential"]).encode("utf-8")
    ).hexdigest()
    sanitization = {
        "schema": "iii.bootstrap-sanitization-required/v1",
        "profile_id": profile["profile_id"],
        "instance_id": instance_id,
        "required": profile["sanitization_contract"],
    }
    user_data = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "ssh_pwauth": False,
        "disable_root": True,
        "package_update": False,
        "package_upgrade": False,
        "users": [
            "default",
            {
                "name": profile["bootstrap_user"],
                "lock_passwd": True,
                "shell": "/bin/bash",
                "groups": ["sudo"],
                "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
                "ssh_authorized_keys": [bootstrap["ssh_public_key"]],
            },
        ],
        "bootcmd": [
            [
                "mkdir",
                "-p",
                "/var/lib/iii/bootstrap",
                "/var/log/iii",
                "/etc/iii-bootstrap",
            ]
        ],
        "write_files": [
            {
                "path": "/etc/iii-bootstrap/credential.sha256",
                "owner": "root:root",
                "permissions": "0600",
                "content": credential_hash + "\n",
            },
            {
                "path": "/etc/iii-bootstrap/sanitization-required.json",
                "owner": "root:root",
                "permissions": "0600",
                "content": json.dumps(sanitization, sort_keys=True) + "\n",
            },
        ],
        "runcmd": [
            [
                "sh",
                "-c",
                "install -D -o root -g adm -m 0640 "
                "/var/log/cloud-init-output.log "
                "/var/log/iii/bootstrap-cloud-init.log && "
                'printf \'%s\\n\' \'{"schema":"iii.cloud-init-bootstrap-status/v1","state":"ansible-ready","instance_id":"'
                + instance_id
                + "\"}' > /var/lib/iii/bootstrap/cloud-init-status.json && chmod 0600 /var/lib/iii/bootstrap/cloud-init-status.json",
            ]
        ],
        "final_message": "III first-boot bootstrap reached ansible-ready; inspect /var/log/cloud-init*.log and /var/log/iii/bootstrap-cloud-init.log on failure.",
    }
    access_points: dict[str, Any] = {}
    for row in bootstrap["network"]["wifi"]:
        settings: dict[str, Any] = {"password": row["password"]}
        if row.get("hidden") is True:
            settings["hidden"] = True
        access_points[row["ssid"]] = settings
    network: dict[str, Any] = {
        "version": 2,
        "ethernets": {
            "operator-usb-ethernet": {
                "match": {"name": profile["ethernet_recovery"]["match_name"]},
                "dhcp4": True,
                "optional": True,
            },
            "px4-ethernet": {
                "match": {"name": profile["px4_ethernet"]["match_name"]},
                "addresses": [profile["px4_ethernet"]["address"]],
                "dhcp4": False,
                "link-local": [],
                "optional": True,
            },
        },
    }
    if access_points:
        network["wifis"] = {
            profile["wifi_interface"]: {
                "dhcp4": True,
                "optional": True,
                "access-points": access_points,
            }
        }
    files = {
        "user-data": _yaml_document(user_data, cloud_config=True),
        "meta-data": _yaml_document(
            {"instance-id": instance_id, "local-hostname": hostname}
        ),
        "network-config": _yaml_document(network),
    }
    return {
        "profile_id": profile["profile_id"],
        "instance_id": instance_id,
        "files": files,
        "file_evidence": [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
            for name, data in sorted(files.items())
        ],
        "contains_network_secret": bool(access_points),
    }


def inspect_image(path: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ImageVerificationError("image must be a real regular file")
    if path.name != source["filename"]:
        raise ImageVerificationError(
            f"image filename must match pinned release identity {source['filename']}"
        )
    compressed_sha = _sha256_file(path)
    if compressed_sha != source["sha256"]:
        raise ImageVerificationError(
            "compressed image SHA-256 does not match the pinned Canonical manifest"
        )
    raw_digest = hashlib.sha256()
    raw_bytes = 0
    try:
        with lzma.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                raw_digest.update(chunk)
                raw_bytes += len(chunk)
    except (lzma.LZMAError, OSError) as exc:
        raise ImageVerificationError(f"cannot decompress pinned image: {exc}") from exc
    if raw_bytes <= 0:
        raise ImageVerificationError("pinned image decompressed to no content")
    return {
        "source_id": source["source_id"],
        "filename": source["filename"],
        "path": str(path.absolute()),
        "release": source["release"],
        "compressed_sha256": compressed_sha,
        "raw_sha256": raw_digest.hexdigest(),
        "raw_bytes": raw_bytes,
        "minimum_target_bytes": max(raw_bytes, int(source["minimum_target_bytes"])),
        "verified": True,
    }


def _flatten_lsblk(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        children = item.pop("children", []) or []
        values.append(item)
        values.extend(_flatten_lsblk(children))
    return values


def _mountpoints(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("mountpoints")
    if raw is None:
        raw = [row.get("mountpoint")]
    return sorted(str(value) for value in raw or [] if value)


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no", ""}:
            return False
    raise UnsafeDeviceError(f"block-device boolean field is ambiguous: {value!r}")


def _stable_paths(kernel_path: str, by_id_root: Path) -> list[str]:
    if not by_id_root.exists() or by_id_root.is_symlink() or not by_id_root.is_dir():
        return []
    target = Path(kernel_path).resolve()
    paths: list[str] = []
    for candidate in sorted(by_id_root.iterdir(), key=lambda item: item.name):
        if "-part" in candidate.name:
            continue
        try:
            if candidate.resolve() == target:
                paths.append(str(candidate))
        except OSError:
            continue
    return paths


def _device_fingerprint(value: Mapping[str, Any]) -> str:
    fields = {
        key: value.get(key)
        for key in (
            "stable_path",
            "kernel_path",
            "model",
            "serial",
            "size",
            "transport",
        )
    }
    return content_identity(fields)


def inspect_devices(
    *,
    minimum_bytes: int,
    lsblk: Mapping[str, Any] | None = None,
    running_sources: Sequence[str] | None = None,
    by_id_root: Path = Path("/dev/disk/by-id"),
) -> list[dict[str, Any]]:
    if lsblk is None:
        try:
            output = subprocess.run(
                [
                    "lsblk",
                    "--json",
                    "--bytes",
                    "--output",
                    "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,PKNAME",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            lsblk = json.loads(output)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise UnsafeDeviceError(
                f"cannot inspect block-device topology: {exc}"
            ) from exc
    rows = _flatten_lsblk(lsblk.get("blockdevices", []))
    if running_sources is None:
        running_sources = []
        for target in ("/", "/boot", "/boot/firmware"):
            result = subprocess.run(
                ["findmnt", "--noheadings", "--output", "SOURCE", "--target", target],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                running_sources.append(result.stdout.strip())
    source_names: set[str] = set()
    for source in running_sources:
        if not source.startswith("/dev/"):
            continue
        source_names.add(Path(source).name)
        try:
            source_names.add(Path(source).resolve().name)
        except OSError:
            pass
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = row.get("pkname")
        if parent:
            by_parent.setdefault(str(parent), []).append(row)

    def descendants(parent: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        pending = list(by_parent.get(parent, []))
        visited: set[str] = set()
        while pending:
            item = pending.pop()
            name = str(item.get("kname") or item.get("name") or "")
            if not name or name in visited:
                raise UnsafeDeviceError(
                    "block-device ancestry is cyclic or missing an identity"
                )
            visited.add(name)
            found.append(item)
            pending.extend(by_parent.get(name, []))
        return found

    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("type") != "disk":
            continue
        kernel_path = str(
            row.get("path") or f"/dev/{row.get('kname') or row.get('name')}"
        )
        kname = str(row.get("kname") or row.get("name"))
        children = descendants(kname)
        stable = _stable_paths(kernel_path, by_id_root)
        if not stable and row.get("stable_path"):
            stable = [str(row["stable_path"])]
        reasons: list[str] = []
        mounted = sorted(
            {point for item in (row, *children) for point in _mountpoints(item)}
        )
        descendant_types = sorted(
            {
                str(item.get("type"))
                for item in children
                if item.get("type") not in {None, "part"}
            }
        )
        running = kname in source_names or any(
            str(item.get("kname") or item.get("name")) in source_names
            for item in children
        )
        removable = _flag(row.get("rm", False))
        readonly = _flag(row.get("ro", False))
        size = int(row.get("size") or 0)
        if running:
            reasons.append("backs-running-system")
        if mounted:
            reasons.append("mounted-or-in-use")
        if descendant_types:
            reasons.append("unresolved-device-mapper-or-holder")
        if not removable:
            reasons.append("not-removable")
        if readonly:
            reasons.append("read-only")
        if size < minimum_bytes:
            reasons.append("smaller-than-pinned-image-contract")
        if not stable:
            reasons.append("no-stable-device-path")
        value: dict[str, Any] = {
            "stable_path": stable[0] if stable else None,
            "alternate_stable_paths": stable[1:],
            "kernel_path": kernel_path,
            "model": str(row.get("model") or "").strip() or None,
            "serial": str(row.get("serial") or "").strip() or None,
            "size": size,
            "transport": str(row.get("tran") or "unknown"),
            "removable": removable,
            "read_only": readonly,
            "mountpoints": mounted,
            "backs_running_system": running,
            "dependent_types": descendant_types,
            "eligible": not reasons,
            "rejection_reasons": reasons,
        }
        value["fingerprint"] = _device_fingerprint(value)
        result.append(value)
    return sorted(result, key=lambda item: (item["stable_path"] or item["kernel_path"]))


def select_device(
    devices: Sequence[Mapping[str, Any]], requested: str
) -> dict[str, Any]:
    matches = [row for row in devices if row.get("stable_path") == requested]
    if len(matches) != 1:
        raise UnsafeDeviceError("select exactly one enumerated /dev/disk/by-id target")
    selected = dict(matches[0])
    if not selected.get("eligible"):
        raise UnsafeDeviceError(
            "selected target is unsafe: "
            + ", ".join(selected.get("rejection_reasons", []))
        )
    return selected


def target_proof_phrase(device: Mapping[str, Any], *, accept_data_loss: bool) -> str:
    prefix = "ERASE AND ACCEPT DATA LOSS" if accept_data_loss else "ERASE"
    return f"{prefix} {device['stable_path']} {device['fingerprint'][:16]}"


def _backup_reference(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = _load_object(path, label="pre-reimage backup record")
    if value.get("schema") not in {
        "iii.host-backup-receipt/v1",
        "iii.host-salvage-record/v1",
    }:
        raise ImagingError(
            "pre-reimage evidence is not a verified host backup or salvage record"
        )
    if value.get("schema") == "iii.host-backup-receipt/v1":
        from .portable_state import validate_external_receipt

        try:
            validate_external_receipt(value)
        except ContractError as exc:
            raise ImagingError(str(exc)) from exc
    elif (
        value.get("verified") is not True
        or value.get("outcome") != "verified"
        or value.get("recommissioning_required") is not True
        or value.get("credentials_recovered") is not False
        or value.get("filesystem", {}).get("source_modified") is not False
    ):
        raise ImagingError("pre-reimage salvage record is incomplete or unsafe")
    elif value.get("salvage_id") != content_identity(
        {key: item for key, item in value.items() if key != "salvage_id"}
    ):
        raise ImagingError("pre-reimage salvage record identity is invalid")
    if value.get("verified") is not True and value.get("outcome") != "verified":
        raise ImagingError("pre-reimage backup record is not verified")
    return {"path": str(path.absolute()), "sha256": _sha256_file(path)}


def _validate_private_output_directory(path: Path) -> Path:
    selected = path.expanduser().absolute()
    if not selected.exists():
        raise ImagingError(
            "evidence directory must be created by the operator before planning"
        )
    for component in (selected, *selected.parents):
        if component.is_symlink():
            raise ImagingError(
                "evidence directory ancestry must not contain symbolic links"
            )
    if not selected.is_dir():
        raise ImagingError("evidence directory must be a directory")
    metadata = selected.stat()
    if hasattr(os, "geteuid") and metadata.st_uid not in _authorized_local_uids():
        raise ImagingError(
            "evidence directory must be owned by the current or invoking sudo user"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ImagingError("evidence directory must not be group/world writable")
    _require_non_repository_or_ignored(selected, label="evidence directory")
    return selected


def build_image_plan(
    *,
    operation_id: str,
    image_path: Path,
    source_path: Path,
    profile_path: Path,
    bootstrap_input_path: Path,
    device_path: str,
    schema_root: Path,
    evidence_directory: Path,
    backup_record: Path | None,
    accept_data_loss: bool,
    lsblk: Mapping[str, Any] | None = None,
    running_sources: Sequence[str] | None = None,
    by_id_root: Path = Path("/dev/disk/by-id"),
) -> dict[str, Any]:
    registry = ContractRegistry(schema_root)
    source = load_contract(
        source_path,
        schema_name="host-image-source",
        registry=registry,
        label="host image source",
    )
    profile = load_contract(
        profile_path,
        schema_name="cloud-init-profile",
        registry=registry,
        label="cloud-init profile",
    )
    if profile["image_source_id"] != source["source_id"]:
        raise ImagingError("cloud-init profile and pinned image source disagree")
    bootstrap = load_bootstrap_input(bootstrap_input_path, registry)
    image = inspect_image(image_path, source)
    devices = inspect_devices(
        minimum_bytes=image["minimum_target_bytes"],
        lsblk=lsblk,
        running_sources=running_sources,
        by_id_root=by_id_root,
    )
    device = select_device(devices, device_path)
    backup = _backup_reference(backup_record)
    if backup is None and not accept_data_loss:
        raise ImagingError(
            "physical reimage requires a verified host backup record or explicit --accept-data-loss"
        )
    seed = render_nocloud_seed(profile=profile, bootstrap=bootstrap)
    plan = {
        "schema": IMAGE_PLAN_SCHEMA,
        "operation_id": operation_id,
        "image": image,
        "source_path": str(source_path.absolute()),
        "profile_path": str(profile_path.absolute()),
        "bootstrap_input_path": str(bootstrap_input_path.absolute()),
        "bootstrap_input_sha256": _sha256_file(bootstrap_input_path),
        "target": device,
        "enumerated_devices": devices,
        "seed": {
            "profile_id": seed["profile_id"],
            "instance_id": seed["instance_id"],
            "files": seed["file_evidence"],
            "contains_network_secret": seed["contains_network_secret"],
        },
        "destructive_authority": {
            "backup_record": backup,
            "accepted_data_loss": accept_data_loss,
            "required_typed_proof": target_proof_phrase(
                device, accept_data_loss=accept_data_loss
            ),
            "unattended_override": False,
        },
        "evidence_directory": str(
            _validate_private_output_directory(evidence_directory)
        ),
        "partition_contract": source["partition_contract"],
    }
    registry.validate("host-image-plan", plan)
    return plan


def _write_raw_image(
    image_path: Path,
    target_path: Path,
    expected: Mapping[str, Any],
    *,
    allow_regular_file: bool,
) -> dict[str, Any]:
    metadata = target_path.stat()
    if not stat.S_ISBLK(metadata.st_mode) and not (
        allow_regular_file and stat.S_ISREG(metadata.st_mode)
    ):
        raise UnsafeDeviceError("the authenticated target is no longer a block device")
    flags = os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_EXCL"):
        flags |= os.O_EXCL
    descriptor = os.open(target_path, flags)
    digest = hashlib.sha256()
    written = 0
    try:
        if allow_regular_file:
            os.ftruncate(descriptor, 0)
        with lzma.open(image_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                view = memoryview(chunk)
                while view:
                    count = os.write(descriptor, view)
                    if count <= 0:
                        raise ImagingError("short write while imaging target")
                    view = view[count:]
                digest.update(chunk)
                written += len(chunk)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if written != expected["raw_bytes"] or digest.hexdigest() != expected["raw_sha256"]:
        raise ImageVerificationError(
            "write stream differs from retained verified image"
        )
    readback = hashlib.sha256()
    remaining = written
    with target_path.open("rb", buffering=0) as stream:
        while remaining:
            chunk = stream.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise ImageVerificationError("target ended before complete readback")
            readback.update(chunk)
            remaining -= len(chunk)
    if readback.hexdigest() != expected["raw_sha256"]:
        raise ImageVerificationError(
            "target readback SHA-256 differs from verified image"
        )
    return {
        "bytes": written,
        "stream_sha256": digest.hexdigest(),
        "readback_sha256": readback.hexdigest(),
        "verified": True,
    }


def _sysfs_partition_number(device_path: str) -> int | None:
    """Return the kernel partition number without depending on lsblk columns."""

    sysfs_value = Path("/sys/class/block") / Path(device_path).name / "partition"
    try:
        return int(sysfs_value.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _partition_one(
    kernel_path: str,
    *,
    partition_number_reader: Callable[[str], int | None] = _sysfs_partition_number,
) -> Path:
    subprocess.run(
        ["partprobe", kernel_path], check=True, capture_output=True, text=True
    )
    last_detail = "partition discovery did not run"
    for attempt in range(PARTITION_DISCOVERY_ATTEMPTS):
        subprocess.run(
            ["udevadm", "settle", "--timeout=1"],
            check=False,
            capture_output=True,
            text=True,
        )
        observed = subprocess.run(
            ["lsblk", "--json", "--output", "PATH,TYPE,FSTYPE", kernel_path],
            check=False,
            capture_output=True,
            text=True,
        )
        if observed.returncode == 0:
            try:
                value = json.loads(observed.stdout)
            except json.JSONDecodeError:
                last_detail = "lsblk returned invalid JSON during device settlement"
            else:
                rows = _flatten_lsblk(value.get("blockdevices", []))
                matches = [
                    row
                    for row in rows
                    if row.get("type") == "part"
                    and partition_number_reader(str(row.get("path") or "")) == 1
                ]
                if len(matches) == 1 and str(
                    matches[0].get("fstype") or ""
                ).lower() in {"vfat", "fat", "fat32"}:
                    return Path(str(matches[0]["path"]))
                last_detail = (
                    "the settled device did not expose exactly one supported FAT "
                    "partition 1"
                )
        else:
            last_detail = "lsblk could not resolve the device during re-enumeration"
        if attempt + 1 < PARTITION_DISCOVERY_ATTEMPTS:
            time.sleep(PARTITION_DISCOVERY_DELAY_SECONDS)
    raise ImagingError(
        "written image did not expose one supported FAT boot partition after "
        f"bounded device settlement: {last_detail}"
    )


def install_seed_on_boot_partition(
    kernel_path: str, seed: Mapping[str, Any]
) -> list[dict[str, Any]]:
    partition = _partition_one(kernel_path)
    payload = json.dumps(
        {
            "schema": "iii.nocloud-seed-transfer/v1",
            "files": {
                name: base64.b64encode(content).decode("ascii")
                for name, content in seed["files"].items()
            },
        },
        sort_keys=True,
    )
    try:
        result = subprocess.run(
            [
                "unshare",
                "--mount",
                "--propagation",
                "private",
                "--kill-child",
                sys.executable,
                "-m",
                "iii_deployment.seed_mount",
                str(partition),
            ],
            input=payload,
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ImagingError(
            f"private-namespace NoCloud seed installation failed: {exc}"
        ) from exc
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(files, list):
        raise ImagingError("private-namespace seed helper returned invalid evidence")
    return files


def flush_and_eject(kernel_path: str) -> dict[str, Any]:
    subprocess.run(
        ["blockdev", "--flushbufs", kernel_path],
        check=True,
        capture_output=True,
        text=True,
    )
    if shutil.which("udisksctl"):
        subprocess.run(
            ["udisksctl", "power-off", "--block-device", kernel_path],
            check=True,
            capture_output=True,
            text=True,
        )
        method = "udisksctl-power-off"
    elif shutil.which("eject"):
        subprocess.run(
            ["eject", kernel_path], check=True, capture_output=True, text=True
        )
        method = "eject"
    else:
        raise ImagingError(
            "neither udisksctl nor eject is available for required safe removal"
        )
    return {
        "fsync": True,
        "block_buffers": True,
        "eject_requested": True,
        "method": method,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ImagingError("evidence directory must be a real directory")
    metadata = path.parent.stat()
    if hasattr(os, "geteuid") and metadata.st_uid not in _authorized_local_uids():
        raise ImagingError(
            "evidence directory must be owned by the current or invoking sudo user"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ImagingError("evidence directory must not be group/world writable")
    serialized = json.dumps(value, sort_keys=True, indent=2) + "\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ImagingError("existing image record path is unsafe")
        if path.read_text(encoding="utf-8") != serialized:
            raise ImagingError(
                "content-addressed image record already exists with different content"
            )
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if hasattr(os, "chown") and metadata.st_uid != os.geteuid():
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def apply_image_plan(
    plan: Mapping[str, Any],
    *,
    schema_root: Path,
    proof_reader: Callable[[str], str] | None = None,
    device_inspector: Callable[..., list[dict[str, Any]]] = inspect_devices,
    image_writer: (
        Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None,
    seed_installer: Callable[
        [str, Mapping[str, Any]], list[dict[str, Any]]
    ] = install_seed_on_boot_partition,
    ejector: Callable[[str], Mapping[str, Any]] = flush_and_eject,
    allow_regular_file: bool = False,
    lsblk: Mapping[str, Any] | None = None,
    running_sources: Sequence[str] | None = None,
    by_id_root: Path = Path("/dev/disk/by-id"),
) -> dict[str, Any]:
    registry = ContractRegistry(schema_root)
    try:
        registry.validate("host-image-plan", plan)
    except Exception as exc:
        raise ImagingError(f"retained host image plan is invalid: {exc}") from exc
    source_path = Path(str(plan["source_path"]))
    profile_path = Path(str(plan["profile_path"]))
    input_path = Path(str(plan["bootstrap_input_path"]))
    source = load_contract(
        source_path,
        schema_name="host-image-source",
        registry=registry,
        label="host image source",
    )
    profile = load_contract(
        profile_path,
        schema_name="cloud-init-profile",
        registry=registry,
        label="cloud-init profile",
    )
    if _sha256_file(input_path) != plan["bootstrap_input_sha256"]:
        raise DeviceChangedError("bootstrap input changed after retained preflight")
    bootstrap = load_bootstrap_input(input_path, registry)
    image_path = Path(str(plan["image"]["path"]))
    observed_image = inspect_image(image_path, source)
    for field in ("source_id", "compressed_sha256", "raw_sha256", "raw_bytes"):
        if observed_image[field] != plan["image"][field]:
            raise DeviceChangedError(f"image {field} changed after retained preflight")
    seed = render_nocloud_seed(profile=profile, bootstrap=bootstrap)
    if seed["file_evidence"] != plan["seed"]["files"]:
        raise DeviceChangedError(
            "rendered NoCloud seed changed after retained preflight"
        )
    devices = device_inspector(
        minimum_bytes=observed_image["minimum_target_bytes"],
        lsblk=lsblk,
        running_sources=running_sources,
        by_id_root=by_id_root,
    )
    current = select_device(devices, str(plan["target"]["stable_path"]))
    if current["fingerprint"] != plan["target"]["fingerprint"]:
        raise DeviceChangedError(
            "selected physical device identity changed after retained preflight"
        )
    required = str(plan["destructive_authority"]["required_typed_proof"])
    reader = input if proof_reader is None else proof_reader
    supplied = reader(f"Type exactly '{required}' to destroy the selected media: ")
    if supplied != required:
        raise TargetProofError("typed physical-device proof did not match")

    write = dict(
        _write_raw_image(
            image_path,
            Path(current["kernel_path"]),
            observed_image,
            allow_regular_file=allow_regular_file,
        )
        if image_writer is None
        else image_writer(image_path, Path(current["kernel_path"]), observed_image)
    )
    if write != {
        "bytes": observed_image["raw_bytes"],
        "stream_sha256": observed_image["raw_sha256"],
        "readback_sha256": observed_image["raw_sha256"],
        "verified": True,
    }:
        raise ImageVerificationError(
            "image writer did not prove exact stream and readback identity"
        )
    stable_target = str(current["stable_path"])
    seed_files = seed_installer(stable_target, seed)
    if seed_files != seed["file_evidence"]:
        raise ImageVerificationError(
            "on-media NoCloud seed evidence differs from retained render"
        )
    flush = dict(ejector(stable_target))
    if not flush.get("block_buffers") or not flush.get("eject_requested"):
        raise ImageVerificationError("media flush/eject did not complete")
    unsigned: dict[str, Any] = {
        "schema": IMAGE_RECORD_SCHEMA,
        "operation_id": plan["operation_id"],
        "recorded_at": _utc_now(),
        "image": {
            key: observed_image[key]
            for key in (
                "source_id",
                "filename",
                "compressed_sha256",
                "raw_sha256",
                "raw_bytes",
            )
        },
        "target": current,
        "destructive_authority": {
            "typed_device_proof": True,
            "backup_record": plan["destructive_authority"]["backup_record"],
            "accepted_data_loss": plan["destructive_authority"]["accepted_data_loss"],
        },
        "write": write,
        "seed": {
            "profile_id": seed["profile_id"],
            "instance_id": seed["instance_id"],
            "files": seed_files,
            "verified": True,
            "contains_network_secret": seed["contains_network_secret"],
        },
        "flush": {"fsync": True, "block_buffers": True},
        "eject": {"requested": True, "method": flush["method"]},
        "outcome": "verified",
    }
    record = {**unsigned, "record_id": content_identity(unsigned)}
    registry.validate("host-image-record", record)
    evidence = Path(str(plan["evidence_directory"])) / f"{record['record_id']}.json"
    _atomic_json(evidence, record)
    return {**record, "evidence_path": str(evidence)}
