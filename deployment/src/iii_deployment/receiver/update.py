"""Signed receiver A/B staging and stable-bootstrap handoff.

The running receiver can verify and populate only the inactive slot.  Selector
mutation is intentionally confined to :class:`ReceiverRecoveryBootstrap`, which
is installed and invoked as stable host infrastructure by Ansible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives import serialization

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.receiver.state import (
    AuditLog,
    OperationJournalStore,
    atomic_document,
    read_boot_id,
)
from iii_deployment.signers import (
    load_private_key,
    load_trusted_signers,
    signer_id_for_public_key,
    trusted_public_key,
    verify,
)

MANIFEST_SCHEMA = "iii.receiver-update-manifest/v1"
STATE_SCHEMA = "iii.receiver-update-state/v1"
READINESS_SCHEMA = "iii.receiver-readiness/v1"
SIGNATURE_SCHEMA = "iii.receiver-update-signature/v1"
SIGNATURE_DOMAIN = b"iii.receiver-update-signature/v1\0"
MANIFEST_NAME = "receiver-update.manifest.json"
ARCHIVE_NAME = "receiver-update.tar"
SIGNATURE_NAME = "receiver-update.sig.json"
RECEIVER_UPDATE_FILES = frozenset({MANIFEST_NAME, ARCHIVE_NAME, SIGNATURE_NAME})
SLOTS = frozenset({"a", "b"})
SELF_UPDATE_DEADLINE_S = 30.0
FORBIDDEN_PAYLOAD_PARTS = frozenset(
    {
        "bootstrap",
        "fallback",
        "systemd",
        "trust",
        "receiver-policy.json",
        "host-maintenance-policy.json",
    }
)


def _identity(value: Mapping[str, Any], field: str) -> str:
    return content_identity({key: item for key, item in value.items() if key != field})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError("receiver update file is not regular")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_symlink(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = path.parent / f".{path.name}.partial-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise ContractError(f"stale receiver selector partial exists: {temporary.name}")
    os.symlink(target, temporary)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ContractError("receiver payload path is malformed")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("receiver payload path escapes its slot")
    if set(path.parts) & FORBIDDEN_PAYLOAD_PARTS:
        raise ContractError(
            "receiver payload attempts to modify stable host infrastructure"
        )
    return path


@dataclass(frozen=True)
class ReceiverCompatibilityInventory:
    bootstrap_protocol: str
    cli_protocol: str
    request_protocol: str
    release_manifest_schema_versions: tuple[str, ...]
    journal_schemas: tuple[str, ...]
    audit_schemas: tuple[str, ...]
    activation_transaction_schemas: tuple[str, ...]
    activation_selector_schemas: tuple[str, ...]
    activation_health_transaction_schemas: tuple[str, ...]
    activation_health_evidence_schemas: tuple[str, ...]
    upload_manifest_schemas: tuple[str, ...]
    upload_activity_schemas: tuple[str, ...]
    configuration_checkpoint_schemas: tuple[str, ...]

    def normalized(self) -> "ReceiverCompatibilityInventory":
        values = asdict(self)
        for field in (
            "release_manifest_schema_versions",
            "journal_schemas",
            "audit_schemas",
            "activation_transaction_schemas",
            "activation_selector_schemas",
            "activation_health_transaction_schemas",
            "activation_health_evidence_schemas",
            "upload_manifest_schemas",
            "upload_activity_schemas",
            "configuration_checkpoint_schemas",
        ):
            values[field] = tuple(sorted(set(values[field])))
        return ReceiverCompatibilityInventory(**values)


@dataclass(frozen=True)
class VerifiedReceiverUpdate:
    directory: Path
    manifest: dict[str, Any]
    signature: dict[str, Any]
    archive_sha256: str


def package_receiver_update(
    payload_root: Path,
    output_directory: Path,
    *,
    generation: int,
    version: str,
    compatibility: Mapping[str, list[str]],
    private_key_path: Path,
    registry: ContractRegistry,
) -> dict[str, Any]:
    """Create one deterministic, separately signed receiver update bundle."""

    if payload_root.is_symlink() or not payload_root.is_dir():
        raise ContractError("receiver payload root is unavailable or linked")
    if output_directory.exists() or output_directory.is_symlink():
        raise ContractError("receiver update output already exists")
    content: list[dict[str, Any]] = []
    for current, directories, files in os.walk(payload_root, followlinks=False):
        directories.sort()
        files.sort()
        base = Path(current)
        for name in directories:
            path = base / name
            if path.is_symlink():
                raise ContractError("receiver payload contains a symbolic link")
            relative = path.relative_to(payload_root).as_posix()
            _safe_relative(relative)
            content.append(
                {
                    "path": relative,
                    "type": "directory",
                    "mode": 0o755,
                    "size": 0,
                    "sha256": None,
                }
            )
        for name in files:
            path = base / name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ContractError("receiver payload contains a link or special file")
            relative = path.relative_to(payload_root).as_posix()
            _safe_relative(relative)
            content.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": 0o755 if metadata.st_mode & 0o111 else 0o644,
                    "size": metadata.st_size,
                    "sha256": _sha256_file(path),
                }
            )
    content.sort(key=lambda item: item["path"].encode("utf-8"))
    executable = next(
        (item for item in content if item["path"] == "bin/iii-deployment-receiver"),
        None,
    )
    if (
        executable is None
        or executable["type"] != "file"
        or executable["mode"] != 0o755
    ):
        raise ContractError("receiver payload lacks its fixed executable")
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "receiver_id": "0" * 64,
        "generation": generation,
        "version": version,
        "content": content,
        "compatibility": {key: list(values) for key, values in compatibility.items()},
    }
    manifest["receiver_id"] = _identity(manifest, "receiver_id")
    registry.validate("receiver-update-manifest", manifest)
    output_directory.mkdir(parents=True, mode=0o700)
    manifest_path = output_directory / MANIFEST_NAME
    archive_path = output_directory / ARCHIVE_NAME
    signature_path = output_directory / SIGNATURE_NAME
    manifest_path.write_bytes(canonical_json(manifest) + b"\n")
    _fsync_file(manifest_path)
    with tarfile.open(archive_path, "x", format=tarfile.USTAR_FORMAT) as archive:
        for item in content:
            info = tarfile.TarInfo(item["path"])
            info.mode = item["mode"]
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.size = item["size"]
            if item["type"] == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                with payload_root.joinpath(*PurePosixPath(item["path"]).parts).open(
                    "rb"
                ) as stream:
                    archive.addfile(info, stream)
    _fsync_file(archive_path)
    key = load_private_key(private_key_path)
    signer_id = signer_id_for_public_key(key.public_key())
    signature: dict[str, Any] = {
        "schema": SIGNATURE_SCHEMA,
        "receiver_id": manifest["receiver_id"],
        "generation": generation,
        "manifest_sha256": _sha256_file(manifest_path),
        "archive_sha256": _sha256_file(archive_path),
        "signer_id": signer_id,
        "authority": "receiver-update",
        "signature": "",
    }
    import base64

    signature["signature"] = base64.b64encode(
        key.sign(_signature_message(signature))
    ).decode("ascii")
    registry.validate("receiver-update-signature", signature)
    signature_path.write_bytes(canonical_json(signature) + b"\n")
    _fsync_file(signature_path)
    _fsync_directory(output_directory)
    verify_receiver_update(
        output_directory,
        trust={
            "schema_version": "1",
            "store_type": "iii.trusted-signers",
            "signers": [
                {
                    "signer_id": signer_id,
                    "algorithm": "Ed25519",
                    "authority": "receiver-update",
                    "public_key": base64.b64encode(
                        key.public_key().public_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PublicFormat.Raw,
                        )
                    ).decode("ascii"),
                    "state": "active",
                }
            ],
        },
        registry=registry,
    )
    return manifest


def _signature_message(signature: Mapping[str, Any]) -> bytes:
    unsigned = dict(signature)
    unsigned.pop("signature", None)
    return SIGNATURE_DOMAIN + canonical_json(unsigned)


def verify_receiver_update(
    directory: Path,
    *,
    trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
) -> VerifiedReceiverUpdate:
    if directory.is_symlink() or not directory.is_dir():
        raise ContractError("receiver update directory is unavailable or linked")
    observed = {path.name for path in directory.iterdir()}
    if observed != RECEIVER_UPDATE_FILES:
        raise ContractError("receiver update bundle has missing or extra files")
    manifest = _read_canonical(
        directory / MANIFEST_NAME, label="receiver update manifest"
    )
    registry.validate("receiver-update-manifest", manifest)
    if manifest["schema"] != MANIFEST_SCHEMA or manifest["receiver_id"] != _identity(
        manifest, "receiver_id"
    ):
        raise ContractError("receiver update logical identity mismatch")
    signature = _read_canonical(
        directory / SIGNATURE_NAME, label="receiver update signature"
    )
    registry.validate("receiver-update-signature", signature)
    if (
        set(signature)
        != {
            "schema",
            "receiver_id",
            "generation",
            "manifest_sha256",
            "archive_sha256",
            "signer_id",
            "authority",
            "signature",
        }
        or signature["schema"] != SIGNATURE_SCHEMA
    ):
        raise ContractError("receiver update signature fields are invalid")
    manifest_sha = _sha256_file(directory / MANIFEST_NAME)
    archive_sha = _sha256_file(directory / ARCHIVE_NAME)
    expected = {
        "receiver_id": manifest["receiver_id"],
        "generation": manifest["generation"],
        "manifest_sha256": manifest_sha,
        "archive_sha256": archive_sha,
        "authority": "receiver-update",
    }
    for field, value in expected.items():
        if signature.get(field) != value:
            raise ContractError(f"receiver update signature {field} disagreement")
    store = load_trusted_signers(trust, registry) if isinstance(trust, Path) else trust
    public = trusted_public_key(store, signature["signer_id"], "receiver-update")
    verify(public, signature["signature"], _signature_message(signature))
    indexed_paths = [item["path"] for item in manifest["content"]]
    if indexed_paths != sorted(
        set(indexed_paths), key=lambda value: value.encode("utf-8")
    ):
        raise ContractError("receiver update content index is unsorted or repeated")
    for item in manifest["content"]:
        _safe_relative(item["path"])
        if item["type"] == "directory" and (
            item["mode"] != 0o755 or item["size"] != 0 or item["sha256"] is not None
        ):
            raise ContractError("receiver directory index semantics are invalid")
        if item["type"] == "file" and (
            item["mode"] not in {0o644, 0o755}
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
        ):
            raise ContractError("receiver file index semantics are invalid")
    for values in manifest["compatibility"].values():
        if values != sorted(set(values)):
            raise ContractError(
                "receiver compatibility values are unsorted or repeated"
            )
    return VerifiedReceiverUpdate(directory, manifest, signature, archive_sha)


def _assert_compatible(
    manifest: Mapping[str, Any], inventory: ReceiverCompatibilityInventory
) -> None:
    compatibility = manifest["compatibility"]
    inventory = inventory.normalized()
    scalar_fields = {
        "bootstrap_protocols": inventory.bootstrap_protocol,
        "cli_protocols": inventory.cli_protocol,
        "request_protocols": inventory.request_protocol,
    }
    failures: list[str] = []
    for supported_field, installed in scalar_fields.items():
        if installed not in compatibility[supported_field]:
            failures.append(f"{supported_field} lacks installed {installed}")
    collection_fields = {
        "release_manifest_schema_versions": inventory.release_manifest_schema_versions,
        "journal_schemas": inventory.journal_schemas,
        "audit_schemas": inventory.audit_schemas,
        "activation_transaction_schemas": inventory.activation_transaction_schemas,
        "activation_selector_schemas": inventory.activation_selector_schemas,
        "activation_health_transaction_schemas": inventory.activation_health_transaction_schemas,
        "activation_health_evidence_schemas": inventory.activation_health_evidence_schemas,
        "upload_manifest_schemas": inventory.upload_manifest_schemas,
        "upload_activity_schemas": inventory.upload_activity_schemas,
        "configuration_checkpoint_schemas": inventory.configuration_checkpoint_schemas,
    }
    for field, installed_values in collection_fields.items():
        unsupported = sorted(set(installed_values) - set(compatibility[field]))
        if unsupported:
            failures.append(f"{field} lacks " + ", ".join(unsupported))
    if failures:
        raise ContractError(
            "receiver compatibility proof failed: " + "; ".join(failures)
        )


def _extract_verified(verified: VerifiedReceiverUpdate, destination: Path) -> None:
    expected = {item["path"]: item for item in verified.manifest["content"]}
    descriptor = os.open(verified.directory / ARCHIVE_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            with tarfile.open(
                fileobj=stream, mode="r:", format=tarfile.USTAR_FORMAT
            ) as archive:
                members = archive.getmembers()
                if [member.name for member in members] != list(expected):
                    raise ContractError(
                        "receiver archive differs from signed content order"
                    )
                for member in members:
                    indexed = expected[member.name]
                    relative = _safe_relative(member.name)
                    if (
                        member.uid != 0
                        or member.gid != 0
                        or member.uname
                        or member.gname
                        or member.mtime != 0
                        or member.pax_headers
                        or member.linkname
                        or member.mode != indexed["mode"]
                        or member.size != indexed["size"]
                    ):
                        raise ContractError(
                            "receiver archive header differs from signed index"
                        )
                    target = destination.joinpath(*relative.parts)
                    if indexed["type"] == "directory":
                        if not member.isdir():
                            raise ContractError("receiver archive entry type differs")
                        target.mkdir(parents=True, exist_ok=False, mode=0o755)
                        continue
                    if not member.isfile():
                        raise ContractError(
                            "receiver archive links and special files are forbidden"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ContractError("receiver archive file cannot be read")
                    digest = hashlib.sha256()
                    output = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        indexed["mode"],
                    )
                    try:
                        copied = 0
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            copied += len(block)
                            digest.update(block)
                            view = memoryview(block)
                            while view:
                                written = os.write(output, view)
                                if written <= 0:
                                    raise ContractError(
                                        "receiver extraction made no write progress"
                                    )
                                view = view[written:]
                        os.fsync(output)
                    finally:
                        os.close(output)
                    if (
                        copied != indexed["size"]
                        or digest.hexdigest() != indexed["sha256"]
                    ):
                        raise ContractError(
                            "receiver archive file differs from signed content"
                        )
    finally:
        os.close(descriptor)
    if _sha256_file(verified.directory / ARCHIVE_NAME) != verified.archive_sha256:
        raise ContractError("receiver archive changed after detached verification")


class ReceiverSlotStore:
    """Stage a verified update without authority to switch receiver selectors."""

    def __init__(
        self,
        root: Path,
        *,
        trust: Path | Mapping[str, Any],
        registry: ContractRegistry,
    ) -> None:
        self.root = root.resolve()
        self.receiver_root = self.root / "opt/iii/receiver"
        self.slots_root = self.receiver_root / "slots"
        self.selector_root = self.receiver_root / "selectors"
        self.current_path = self.selector_root / "current"
        self.fallback_path = self.selector_root / "fallback"
        self.bootstrap_root = self.receiver_root / "bootstrap"
        self.state_path = self.root / "var/lib/iii/deployment/receiver-update.json"
        self.operation_root = self.root / "var/lib/iii/deployment"
        self.activation_transaction_root = (
            self.operation_root / "activation-transactions"
        )
        self.activation_selector_path = self.operation_root / "active-selector.json"
        self.activation_health_root = self.operation_root / "activation"
        self.incoming_root = self.root / "var/lib/iii/incoming"
        self.configuration_checkpoint_root = (
            self.root / "var/lib/iii/configuration/checkpoints"
        )
        self.release_root = self.root / "opt/iii/releases"
        self.audit_path = self.root / "var/log/iii/deployment/receiver-audit.jsonl"
        self.bootstrap_protocol_path = self.bootstrap_root / "protocol.json"
        self.cli_protocol_path = self.root / "etc/iii/receiver-cli-protocol.json"
        self.trust = trust
        self.registry = registry
        for path, label in (
            (self.receiver_root, "receiver root"),
            (self.slots_root, "receiver slots root"),
            (self.selector_root, "receiver selector root"),
            (self.bootstrap_root, "receiver bootstrap root"),
        ):
            if path.is_symlink():
                raise ContractError(f"{label} cannot be a symbolic link")

    @staticmethod
    def _protocol(path: Path, *, schema: str, label: str) -> str:
        value = _read_canonical(path, label=label)
        if set(value) != {"schema", "protocol"} or value["schema"] != schema:
            raise ContractError(f"{label} fields are invalid")
        protocol = value["protocol"]
        if not isinstance(protocol, str) or not protocol:
            raise ContractError(f"{label} protocol is invalid")
        return protocol

    @staticmethod
    def _canonical_documents(root: Path, *, label: str) -> list[dict[str, Any]]:
        if not root.exists() and not root.is_symlink():
            return []
        if root.is_symlink() or not root.is_dir():
            raise ContractError(f"{label} root is linked or invalid")
        values: list[dict[str, Any]] = []
        for path in sorted(root.iterdir()):
            if path.is_symlink():
                raise ContractError(f"{label} contains a symbolic link")
            if path.is_dir():
                path = path / "checkpoint.json"
            if not path.is_file() or path.suffix != ".json":
                raise ContractError(f"{label} contains an unknown entry")
            values.append(_read_canonical(path, label=label))
        return values

    @staticmethod
    def _nested_canonical_documents(root: Path, *, label: str) -> list[dict[str, Any]]:
        if not root.exists() and not root.is_symlink():
            return []
        if root.is_symlink() or not root.is_dir():
            raise ContractError(f"{label} root is linked or invalid")
        values: list[dict[str, Any]] = []
        for current, directories, files in os.walk(root, followlinks=False):
            directories.sort()
            files.sort()
            base = Path(current)
            for name in directories:
                if (base / name).is_symlink():
                    raise ContractError(f"{label} contains a symbolic link")
            for name in files:
                path = base / name
                if path.is_symlink() or path.suffix != ".json":
                    raise ContractError(f"{label} contains an unsafe entry")
                values.append(_read_canonical(path, label=label))
        return values

    def inspect_compatibility_inventory(self) -> ReceiverCompatibilityInventory:
        """Authenticate every installed format the candidate must preserve."""

        release_versions: list[str] = []
        if self.release_root.exists() or self.release_root.is_symlink():
            if self.release_root.is_symlink() or not self.release_root.is_dir():
                raise ContractError("retained release root is linked or invalid")
            for slot in sorted(self.release_root.iterdir()):
                if slot.is_symlink() or not slot.is_dir():
                    raise ContractError(
                        "retained release inventory contains an unsafe slot"
                    )
                manifest = _read_canonical(
                    slot / "release-manifest.json",
                    label="retained release manifest",
                )
                self.registry.validate("release-manifest", manifest)
                release_versions.append(str(manifest["schema_version"]))
        journals = OperationJournalStore(
            self.operation_root,
            monotonic=lambda: 0.0,
            boot_id=lambda: "inventory",
        ).list()
        audits = AuditLog(
            self.audit_path,
            monotonic=lambda: 0.0,
            boot_id=lambda: "inventory",
        ).entries()
        transactions = self._canonical_documents(
            self.activation_transaction_root,
            label="activation transaction",
        )
        for value in transactions:
            self.registry.validate("activation-transaction", value)
        selectors: list[dict[str, Any]] = []
        if (
            self.activation_selector_path.exists()
            or self.activation_selector_path.is_symlink()
        ):
            selector = _read_canonical(
                self.activation_selector_path,
                label="active deployment selector",
            )
            self.registry.validate("activation-selector", selector)
            selectors.append(selector)
        activation_health_transactions = self._canonical_documents(
            self.activation_health_root / "transactions",
            label="activation health transaction",
        )
        for value in activation_health_transactions:
            self.registry.validate("activation-health-transaction", value)
        activation_health_evidence = self._nested_canonical_documents(
            self.activation_health_root / "evidence",
            label="activation health evidence",
        )
        for value in activation_health_evidence:
            self.registry.validate("activation-health", value)
        upload_manifests: list[dict[str, Any]] = []
        upload_activities: list[dict[str, Any]] = []
        if self.incoming_root.exists() or self.incoming_root.is_symlink():
            if self.incoming_root.is_symlink() or not self.incoming_root.is_dir():
                raise ContractError("incoming upload root is linked or invalid")
            for partial in sorted(self.incoming_root.glob("*.partial")):
                if partial.is_symlink() or not partial.is_dir():
                    raise ContractError("incoming upload partial is linked or invalid")
                manifest_path = partial / ".upload-manifest.json"
                if manifest_path.exists() or manifest_path.is_symlink():
                    value = _read_canonical(
                        manifest_path, label="incoming upload manifest"
                    )
                    if re.fullmatch(r"[a-f0-9]{64}\.partial", partial.name):
                        self.registry.validate("bundle-upload", value)
                        upload_manifests.append(value)
                    elif re.fullmatch(
                        r"receiver-[a-f0-9]{64}\.partial", partial.name
                    ):
                        self.registry.validate("receiver-update-upload", value)
                    elif re.fullmatch(
                        r"backup-[a-f0-9]{64}\.partial", partial.name
                    ):
                        self.registry.validate("portable-backup-upload", value)
                    else:
                        raise ContractError(
                            "incoming upload partial has an unsupported identity"
                        )
                activity_path = partial / ".upload-activity.json"
                if activity_path.exists() or activity_path.is_symlink():
                    value = _read_canonical(
                        activity_path, label="incoming upload activity"
                    )
                    self.registry.validate("bundle-upload-activity", value)
                    upload_activities.append(value)
        checkpoints = self._canonical_documents(
            self.configuration_checkpoint_root,
            label="configuration checkpoint",
        )
        return ReceiverCompatibilityInventory(
            bootstrap_protocol=self._protocol(
                self.bootstrap_protocol_path,
                schema="iii.receiver-bootstrap-protocol/v1",
                label="receiver bootstrap protocol",
            ),
            cli_protocol=self._protocol(
                self.cli_protocol_path,
                schema="iii.receiver-cli-protocol/v1",
                label="receiver CLI protocol",
            ),
            request_protocol="1",
            release_manifest_schema_versions=tuple(release_versions),
            journal_schemas=tuple(str(value["schema"]) for value in journals),
            audit_schemas=tuple(str(value["schema"]) for value in audits),
            activation_transaction_schemas=tuple(
                str(value["schema"]) for value in transactions
            ),
            activation_selector_schemas=tuple(
                str(value["schema"]) for value in selectors
            ),
            activation_health_transaction_schemas=tuple(
                str(value["schema"]) for value in activation_health_transactions
            ),
            activation_health_evidence_schemas=tuple(
                str(value["schema"]) for value in activation_health_evidence
            ),
            upload_manifest_schemas=tuple(
                str(value["schema"]) for value in upload_manifests
            ),
            upload_activity_schemas=tuple(
                str(value["schema"]) for value in upload_activities
            ),
            configuration_checkpoint_schemas=tuple(
                str(value.get("schema", "")) for value in checkpoints
            ),
        ).normalized()

    def _selector_slot(self, path: Path, *, required: bool) -> str | None:
        if not path.exists() and not path.is_symlink():
            if required:
                raise ContractError(f"receiver selector is missing: {path.name}")
            return None
        if not path.is_symlink():
            raise ContractError(
                f"receiver selector is not a symbolic link: {path.name}"
            )
        resolved = path.resolve(strict=True)
        if (
            not resolved.is_relative_to(self.slots_root.resolve())
            or resolved.parent != self.slots_root.resolve()
        ):
            raise ContractError(f"receiver selector escapes fixed slots: {path.name}")
        if resolved.name not in SLOTS:
            raise ContractError(f"receiver selector names an unknown slot: {path.name}")
        return resolved.name

    def active_slot(self) -> str | None:
        return self._selector_slot(self.current_path, required=False)

    def inactive_slot(self) -> str:
        return "b" if self.active_slot() == "a" else "a"

    def verify_update(self, bundle: Path) -> VerifiedReceiverUpdate:
        """Verify one signed update against this store's fixed trust contract."""

        return verify_receiver_update(
            bundle, trust=self.trust, registry=self.registry
        )

    def update_state(self) -> dict[str, Any] | None:
        """Return the authenticated durable A/B handoff state when present."""

        if not self.state_path.exists() and not self.state_path.is_symlink():
            return None
        value = _read_canonical(self.state_path, label="receiver update state")
        if value.get("schema") != STATE_SCHEMA or value.get("state_id") != _identity(
            value, "state_id"
        ):
            raise ContractError("receiver update state identity mismatch")
        self.registry.validate("receiver-update-state", value)
        return value

    def abort_staged(
        self, *, operation_id: str, client_id: str, reason: str
    ) -> dict[str, Any]:
        """Close a pre-switch update after the external handoff cannot start."""

        value = self.update_state()
        if value is None:
            raise ContractError("receiver update state is unavailable for abort")
        if (
            value["stage"] != "staged"
            or value["operation_id"] != operation_id
            or value["client_id"] != client_id
        ):
            raise ContractError("receiver update abort binding or stage mismatch")
        if not isinstance(reason, str) or not reason:
            raise ContractError("receiver update abort reason is empty")
        value["stage"] = "reverted"
        value["failure"] = reason
        value["readiness"] = None
        value["state_id"] = _identity(value, "state_id")
        self.registry.validate("receiver-update-state", value)
        atomic_document(self.state_path, value)
        return value

    def install_initial(self, bundle: Path) -> dict[str, Any]:
        """Install the first signed receiver on a clean converged host.

        This narrow Ansible bootstrap path has no update authority once a
        different receiver is selected.  Every interrupted stage is either a
        verified slot ``a`` that can be completed or an owned partial that can
        be discarded before selectors exist.
        """

        verified = self.verify_update(bundle)
        compatibility = verified.manifest["compatibility"]
        if (
            "1" not in compatibility["bootstrap_protocols"]
            or "1" not in compatibility["cli_protocols"]
            or "1" not in compatibility["request_protocols"]
        ):
            raise ContractError(
                "initial receiver does not support the fixed bootstrap, CLI, and request protocols"
            )
        self.slots_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        self.selector_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        unknown = [
            path
            for path in self.slots_root.iterdir()
            if path.name not in {"a", "b"} and not path.name.startswith(".initial-")
        ]
        if unknown:
            raise ContractError("receiver slots root contains an unknown entry")
        for partial in sorted(self.slots_root.glob(".initial-*")):
            if partial.is_symlink() or not partial.is_dir():
                raise ContractError("initial receiver partial has an unsafe type")
            if self.current_path.exists() or self.current_path.is_symlink():
                raise ContractError(
                    "initial receiver partial remains after selector creation"
                )
            self._make_removable(partial)
            shutil.rmtree(partial)

        # A later Ansible convergence installs the current stable bootstrap
        # before reaching this operation.  If A/B selectors already establish
        # a complete receiver, authenticate and preserve that installation
        # instead of trying to force the historical generation-1 bundle back
        # into slot a.  This path never changes either selector or slot.
        current = self.active_slot()
        if current is not None:
            fallback = self._selector_slot(self.fallback_path, required=True)
            current_manifest = self.verify_slot(current)
            self.verify_slot(fallback)
            current_compatibility = current_manifest["compatibility"]
            if (
                "1" not in current_compatibility["bootstrap_protocols"]
                or "1" not in current_compatibility["cli_protocols"]
                or "1" not in current_compatibility["request_protocols"]
            ):
                raise ContractError(
                    "installed receiver does not support the fixed bootstrap, CLI, and request protocols"
                )
            return {
                "schema": "iii.initial-receiver-install/v1",
                "receiver_id": current_manifest["receiver_id"],
                "generation": current_manifest["generation"],
                "slot": current,
                "current": current,
                "fallback": fallback,
                "installed": False,
            }

        destination = self.slots_root / "a"
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ContractError("initial receiver slot has an unsafe type")
            observed = self.verify_slot("a")
            if observed["receiver_id"] != verified.manifest["receiver_id"]:
                raise ContractError("a different initial receiver is already installed")
        else:
            if (self.slots_root / "b").exists() or (self.slots_root / "b").is_symlink():
                raise ContractError(
                    "receiver slot b exists before initial installation"
                )
            temporary = Path(tempfile.mkdtemp(prefix=".initial-", dir=self.slots_root))
            try:
                _extract_verified(verified, temporary)
                (temporary / MANIFEST_NAME).write_bytes(
                    canonical_json(verified.manifest) + b"\n"
                )
                _fsync_file(temporary / MANIFEST_NAME)
                self._freeze(temporary)
                os.replace(temporary, destination)
                _fsync_directory(self.slots_root)
                self.verify_slot("a")
            except Exception:
                self._make_removable(temporary)
                shutil.rmtree(temporary, ignore_errors=True)
                raise

        current = self.active_slot()
        if current not in {None, "a"}:
            raise ContractError("initial receiver cannot replace an active receiver")
        if current is None:
            _atomic_symlink(self.current_path, destination)
        fallback = self._selector_slot(self.fallback_path, required=False)
        if fallback not in {None, "a"}:
            raise ContractError("initial receiver cannot replace receiver fallback")
        if fallback is None:
            _atomic_symlink(self.fallback_path, destination)
        self.verify_slot("a")
        return {
            "schema": "iii.initial-receiver-install/v1",
            "receiver_id": verified.manifest["receiver_id"],
            "generation": verified.manifest["generation"],
            "slot": "a",
            "current": "a",
            "fallback": "a",
            "installed": current is None,
        }

    def slot_manifest(self, slot: str) -> dict[str, Any]:
        if slot not in SLOTS:
            raise ContractError("unknown receiver slot")
        manifest = _read_canonical(
            self.slots_root / slot / MANIFEST_NAME,
            label=f"receiver slot {slot} manifest",
        )
        self.registry.validate("receiver-update-manifest", manifest)
        if manifest["receiver_id"] != _identity(manifest, "receiver_id"):
            raise ContractError("installed receiver manifest identity mismatch")
        return manifest

    def verify_slot(self, slot: str) -> dict[str, Any]:
        manifest = self.slot_manifest(slot)
        root = self.slots_root / slot
        expected = {item["path"]: item for item in manifest["content"]}
        observed: dict[str, str] = {}
        for current, directories, files in os.walk(root, followlinks=False):
            directories.sort()
            files.sort()
            base = Path(current)
            for name in directories:
                path = base / name
                if path.is_symlink():
                    raise ContractError("installed receiver contains a symbolic link")
                observed[path.relative_to(root).as_posix()] = "directory"
            for name in files:
                path = base / name
                if path.is_symlink() or not path.is_file():
                    raise ContractError("installed receiver contains an unsafe file")
                observed[path.relative_to(root).as_posix()] = "file"
        if set(observed) != set(expected) | {MANIFEST_NAME}:
            raise ContractError("installed receiver has missing or extra content")
        for relative, item in expected.items():
            path = root.joinpath(*PurePosixPath(relative).parts)
            if observed[relative] != item["type"]:
                raise ContractError("installed receiver content type mismatch")
            if item["type"] == "file" and (
                path.stat().st_size != item["size"]
                or _sha256_file(path) != item["sha256"]
            ):
                raise ContractError("installed receiver content hash mismatch")
        return manifest

    @staticmethod
    def _freeze(root: Path) -> None:
        for current, directories, files in os.walk(
            root, topdown=False, followlinks=False
        ):
            base = Path(current)
            for name in files:
                path = base / name
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            for name in directories:
                (base / name).chmod(0o555)
        root.chmod(0o555)

    @staticmethod
    def _make_removable(root: Path) -> None:
        if root.is_symlink() or not root.exists():
            return
        for current, directories, files in os.walk(
            root, topdown=False, followlinks=False
        ):
            base = Path(current)
            for name in files:
                path = base / name
                if not path.is_symlink():
                    path.chmod(0o600)
            for name in directories:
                path = base / name
                if not path.is_symlink():
                    path.chmod(0o700)
        root.chmod(0o700)

    def stage(
        self,
        bundle: Path,
        *,
        inventory: ReceiverCompatibilityInventory | None = None,
        operation_id: str,
        client_id: str,
    ) -> dict[str, Any]:
        verified = self.verify_update(bundle)
        if inventory is not None and self.root == Path("/"):
            raise ContractError(
                "production receiver compatibility cannot be caller-supplied"
            )
        observed_inventory = inventory or self.inspect_compatibility_inventory()
        _assert_compatible(verified.manifest, observed_inventory)
        active = self.active_slot()
        if active is not None:
            current = self.slot_manifest(active)
            if verified.manifest["generation"] <= current["generation"]:
                raise ContractError(
                    "receiver update generation is not newer than active"
                )
        inactive = self.inactive_slot()
        destination = self.slots_root / inactive
        if self.fallback_path.exists() or self.fallback_path.is_symlink():
            fallback = self._selector_slot(self.fallback_path, required=True)
            if fallback == inactive:
                raise ContractError("inactive receiver slot is protected by fallback")
        self.slots_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        temporary = Path(tempfile.mkdtemp(prefix=f".{inactive}.", dir=self.slots_root))
        try:
            _extract_verified(verified, temporary)
            (temporary / MANIFEST_NAME).write_bytes(
                canonical_json(verified.manifest) + b"\n"
            )
            with (temporary / MANIFEST_NAME).open("rb") as stream:
                os.fsync(stream.fileno())
            executable = temporary / "bin/iii-deployment-receiver"
            if (
                executable.is_symlink()
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise ContractError("receiver update lacks its fixed executable")
            self._freeze(temporary)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise ContractError("inactive receiver slot has an unsafe type")
                self._make_removable(destination)
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            _fsync_directory(self.slots_root)
            self.verify_slot(inactive)
        except Exception:
            self._make_removable(temporary)
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        state: dict[str, Any] = {
            "schema": STATE_SCHEMA,
            "state_id": "0" * 64,
            "operation_id": operation_id,
            "client_id": client_id,
            "candidate_slot": inactive,
            "candidate_receiver_id": verified.manifest["receiver_id"],
            "candidate_generation": verified.manifest["generation"],
            "previous_slot": active,
            "stage": "staged",
            "remaining_deadline_s": SELF_UPDATE_DEADLINE_S,
            "budget_boot_id": read_boot_id(),
            "budget_monotonic": 0.0,
            "failure": None,
            "readiness": None,
            "application_activation_started": False,
        }
        state["state_id"] = _identity(state, "state_id")
        self.registry.validate("receiver-update-state", state)
        atomic_document(self.state_path, state)
        return state


class ReceiverRecoveryBootstrap:
    """Stable host bootstrap that alone switches and reverts receiver slots."""

    def __init__(
        self,
        slots: ReceiverSlotStore,
        *,
        monotonic: Callable[[], float],
        boot_id: Callable[[], str],
        restart_receiver: Callable[[], None],
        readiness_probe: Callable[[], Mapping[str, Any]],
        wait_tick: Callable[[], None],
    ) -> None:
        self.slots = slots
        self.monotonic = monotonic
        self.boot_id = boot_id
        self.restart_receiver = restart_receiver
        self.readiness_probe = readiness_probe
        self.wait_tick = wait_tick

    def _load(self) -> dict[str, Any]:
        value = _read_canonical(self.slots.state_path, label="receiver update state")
        if value.get("schema") != STATE_SCHEMA or value.get("state_id") != _identity(
            value, "state_id"
        ):
            raise ContractError("receiver update state identity mismatch")
        self.slots.registry.validate("receiver-update-state", value)
        return value

    def prepare_staging(self) -> str:
        """Advance fallback to current before the old inactive slot is replaced."""

        active = self.slots.active_slot()
        if active is None:
            raise ContractError(
                "receiver bootstrap cannot prepare without an active slot"
            )
        if self.slots.state_path.exists() or self.slots.state_path.is_symlink():
            retained = self._load()
            if retained["stage"] not in {"committed", "reverted"}:
                raise ContractError("receiver update is already nonterminal")
        self.slots.verify_slot(active)
        _atomic_symlink(self.slots.fallback_path, self.slots.slots_root / active)
        return "b" if active == "a" else "a"

    def _save(self, value: dict[str, Any], stage: str) -> dict[str, Any]:
        value["stage"] = stage
        value["state_id"] = _identity(value, "state_id")
        self.slots.registry.validate("receiver-update-state", value)
        atomic_document(self.slots.state_path, value)
        return value

    def _charge(self, value: dict[str, Any]) -> None:
        boot = self.boot_id()
        now = self.monotonic()
        if value["budget_boot_id"] == boot and value["budget_monotonic"] > 0:
            value["remaining_deadline_s"] = max(
                0.0,
                float(value["remaining_deadline_s"])
                - max(0.0, now - float(value["budget_monotonic"])),
            )
        value["budget_boot_id"] = boot
        value["budget_monotonic"] = now

    def _revert(self, value: dict[str, Any], message: str) -> dict[str, Any]:
        previous = value["previous_slot"]
        if previous not in SLOTS:
            raise ContractError("receiver update cannot revert without a previous slot")
        self._save(value, "revert-prepared")
        _atomic_symlink(self.slots.current_path, self.slots.slots_root / previous)
        try:
            self.restart_receiver()
        except Exception as exc:
            message = f"{message}; fallback restart deferred to systemd: {exc}"
        value["failure"] = message
        value["readiness"] = None
        return self._save(value, "reverted")

    def _readiness(self, value: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            observed = dict(self.readiness_probe())
        except Exception:
            return None
        required = {
            "schema",
            "receiver_id",
            "generation",
            "socket_open",
            "self_tests_passed",
            "journal_compatible",
            "bootstrap_protocol",
            "cli_protocol",
            "request_protocol",
        }
        if set(observed) != required or observed.get("schema") != READINESS_SCHEMA:
            return None
        try:
            self.slots.registry.validate("receiver-readiness", observed)
        except ContractError:
            return None
        if (
            observed["receiver_id"] != value["candidate_receiver_id"]
            or observed["generation"] != value["candidate_generation"]
            or observed["bootstrap_protocol"] != "1"
            or observed["cli_protocol"] != "1"
            or observed["request_protocol"] != "1"
            or not observed["socket_open"]
            or not observed["self_tests_passed"]
            or not observed["journal_compatible"]
        ):
            return None
        return observed

    def apply(self) -> dict[str, Any]:
        value = self._load()
        if value["stage"] not in {"staged", "selector-switched", "candidate-started"}:
            raise ContractError("receiver update is not switchable")
        candidate = self.slots.slots_root / value["candidate_slot"]
        self.slots.verify_slot(value["candidate_slot"])
        if value["stage"] == "staged":
            if self.slots.active_slot() != value["previous_slot"]:
                raise ContractError(
                    "receiver update active selector changed after staging"
                )
            self._charge(value)
            self._save(value, "switch-prepared")
            if value["previous_slot"] is not None:
                _atomic_symlink(
                    self.slots.fallback_path,
                    self.slots.slots_root / value["previous_slot"],
                )
            _atomic_symlink(self.slots.current_path, candidate)
            self._save(value, "selector-switched")
        try:
            self.restart_receiver()
        except Exception as exc:
            return self._revert(value, f"receiver candidate failed to start: {exc}")
        self._save(value, "candidate-started")
        while True:
            self._charge(value)
            readiness = self._readiness(value)
            if readiness is not None:
                value["readiness"] = readiness
                value["failure"] = None
                return self._save(value, "committed")
            if value["remaining_deadline_s"] <= 0:
                return self._revert(value, "receiver readiness deadline expired")
            self._save(value, "candidate-started")
            self.wait_tick()

    def reconcile(self) -> dict[str, Any]:
        value = self._load()
        if value["stage"] in {"committed", "reverted", "staged"}:
            return value
        if value["stage"] == "revert-prepared":
            return self._revert(
                value, value.get("failure") or "interrupted receiver reversion"
            )
        if value["stage"] in {
            "switch-prepared",
            "selector-switched",
            "candidate-started",
        }:
            self._charge(value)
            if value["remaining_deadline_s"] <= 0:
                return self._revert(
                    value, "receiver readiness deadline expired after reboot"
                )
            if self.slots.active_slot() != value["candidate_slot"]:
                _atomic_symlink(
                    self.slots.current_path,
                    self.slots.slots_root / value["candidate_slot"],
                )
            if value["stage"] == "switch-prepared":
                self._save(value, "selector-switched")
            return self.apply()
        raise ContractError("receiver update state has an unknown reconciliation stage")

    def assert_application_pair_compatible(
        self,
        *,
        release_manifest_schema_version: str,
        configuration_checkpoint_schema: str,
    ) -> None:
        active = self.slots.active_slot()
        if active is None:
            raise ContractError("receiver active slot is unavailable")
        manifest = self.slots.slot_manifest(active)
        compatibility = manifest["compatibility"]
        if (
            release_manifest_schema_version
            not in compatibility["release_manifest_schema_versions"]
        ):
            raise ContractError(
                "active receiver cannot manage restored release manifest"
            )
        if (
            configuration_checkpoint_schema
            not in compatibility["configuration_checkpoint_schemas"]
        ):
            raise ContractError(
                "active receiver cannot restore configuration checkpoint"
            )
