from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tarfile

import pytest

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.receiver.state import atomic_document
from iii_deployment.receiver.update import (
    ARCHIVE_NAME,
    MANIFEST_NAME,
    READINESS_SCHEMA,
    SIGNATURE_DOMAIN,
    SIGNATURE_NAME,
    SIGNATURE_SCHEMA,
    ReceiverCompatibilityInventory,
    ReceiverRecoveryBootstrap,
    ReceiverSlotStore,
    package_receiver_update,
    verify_receiver_update,
)
from iii_deployment.signers import generate_signer, sign

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas/v1"
REGISTRY = ContractRegistry(SCHEMAS)


class Clock:
    def __init__(self):
        self.value = 100.0
        self.boot = "boot-a"

    def monotonic(self):
        return self.value

    def boot_id(self):
        return self.boot

    def tick(self):
        self.value += 10.0


def _trust(tmp_path: Path):
    key = tmp_path / "receiver.pem"
    descriptor_path = tmp_path / "receiver-public.json"
    descriptor = generate_signer(
        key,
        descriptor_path,
        authority="receiver-update",
        registry=REGISTRY,
    )
    store = {
        "schema_version": "1",
        "store_type": "iii.trusted-signers",
        "signers": [
            {
                "signer_id": descriptor["signer_id"],
                "algorithm": "Ed25519",
                "authority": "receiver-update",
                "public_key": descriptor["public_key"],
                "state": "active",
            }
        ],
    }
    REGISTRY.validate("trusted-signers", store)
    return key, store


def _content(root: Path):
    values = []
    for current, directories, files in os.walk(root):
        directories.sort()
        files.sort()
        base = Path(current)
        for name in directories:
            path = base / name
            values.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "directory",
                    "mode": 0o755,
                    "size": 0,
                    "sha256": None,
                }
            )
        for name in files:
            path = base / name
            raw = path.read_bytes()
            values.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "file",
                    "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    return sorted(values, key=lambda item: item["path"].encode())


def _bundle(
    tmp_path: Path,
    key: Path,
    *,
    generation: int,
    compatibility_overrides=None,
    forbidden_file: str | None = None,
):
    root = tmp_path / f"payload-{generation}"
    executable = root / "bin/iii-deployment-receiver"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!/bin/sh\n# receiver generation {generation}\n")
    executable.chmod(0o755)
    marker = root / "share/generation.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text(f"{generation}\n")
    if forbidden_file:
        path = root / forbidden_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("forbidden\n")
    content = _content(root)
    compatibility = {
        "bootstrap_protocols": ["1"],
        "cli_protocols": ["1"],
        "request_protocols": ["1"],
        "release_manifest_schema_versions": ["1"],
        "journal_schemas": ["iii.receiver-operation-journal/v1"],
        "audit_schemas": ["iii.receiver-audit/v1"],
        "activation_transaction_schemas": ["iii.activation-transaction/v1"],
        "activation_selector_schemas": ["iii.activation-selector/v1"],
        "activation_health_transaction_schemas": [
            "iii.activation-health-transaction/v1"
        ],
        "activation_health_evidence_schemas": ["iii.activation-health/v1"],
        "configuration_checkpoint_schemas": ["iii.configuration-checkpoint/v1"],
    }
    compatibility.update(compatibility_overrides or {})
    manifest = {
        "schema": "iii.receiver-update-manifest/v1",
        "receiver_id": "0" * 64,
        "generation": generation,
        "version": f"v1.0.{generation}",
        "content": content,
        "compatibility": compatibility,
    }
    manifest["receiver_id"] = content_identity(
        {key: value for key, value in manifest.items() if key != "receiver_id"}
    )
    bundle = tmp_path / f"bundle-{generation}"
    bundle.mkdir()
    (bundle / MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
    with tarfile.open(
        bundle / ARCHIVE_NAME, "w", format=tarfile.USTAR_FORMAT
    ) as archive:
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
                with (root / item["path"]).open("rb") as stream:
                    archive.addfile(info, stream)
    signature = {
        "schema": SIGNATURE_SCHEMA,
        "receiver_id": manifest["receiver_id"],
        "generation": generation,
        "manifest_sha256": hashlib.sha256(
            (bundle / MANIFEST_NAME).read_bytes()
        ).hexdigest(),
        "archive_sha256": hashlib.sha256(
            (bundle / ARCHIVE_NAME).read_bytes()
        ).hexdigest(),
        "signer_id": "",
        "authority": "receiver-update",
        "signature": "",
    }
    unsigned = dict(signature)
    unsigned.pop("signature")
    signer_id, encoded = sign(key, SIGNATURE_DOMAIN + canonical_json(unsigned))
    signature["signer_id"] = signer_id
    unsigned["signer_id"] = signer_id
    _unused, encoded = sign(key, SIGNATURE_DOMAIN + canonical_json(unsigned))
    signature["signature"] = encoded
    (bundle / SIGNATURE_NAME).write_bytes(canonical_json(signature) + b"\n")
    return bundle, manifest


def _inventory(**overrides):
    values = {
        "bootstrap_protocol": "1",
        "cli_protocol": "1",
        "request_protocol": "1",
        "release_manifest_schema_versions": ("1",),
        "journal_schemas": ("iii.receiver-operation-journal/v1",),
        "audit_schemas": ("iii.receiver-audit/v1",),
        "activation_transaction_schemas": ("iii.activation-transaction/v1",),
        "activation_selector_schemas": ("iii.activation-selector/v1",),
        "activation_health_transaction_schemas": (
            "iii.activation-health-transaction/v1",
        ),
        "activation_health_evidence_schemas": ("iii.activation-health/v1",),
        "configuration_checkpoint_schemas": ("iii.configuration-checkpoint/v1",),
    }
    values.update(overrides)
    return ReceiverCompatibilityInventory(**values)


def _readiness(manifest, **overrides):
    value = {
        "schema": READINESS_SCHEMA,
        "receiver_id": manifest["receiver_id"],
        "generation": manifest["generation"],
        "socket_open": True,
        "self_tests_passed": True,
        "journal_compatible": True,
        "bootstrap_protocol": "1",
        "cli_protocol": "1",
        "request_protocol": "1",
    }
    value.update(overrides)
    return value


def _resign_archive(bundle: Path, key: Path):
    signature = json.loads((bundle / SIGNATURE_NAME).read_text())
    signature["archive_sha256"] = hashlib.sha256(
        (bundle / ARCHIVE_NAME).read_bytes()
    ).hexdigest()
    unsigned = dict(signature)
    unsigned.pop("signature")
    signer_id, encoded = sign(key, SIGNATURE_DOMAIN + canonical_json(unsigned))
    assert signer_id == signature["signer_id"]
    signature["signature"] = encoded
    (bundle / SIGNATURE_NAME).write_bytes(canonical_json(signature) + b"\n")


def test_production_packager_is_deterministic_and_self_verifying(tmp_path):
    key, trust = _trust(tmp_path)
    payload = tmp_path / "payload"
    executable = payload / "bin/iii-deployment-receiver"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    compatibility = _bundle(tmp_path, key, generation=9)[1]["compatibility"]
    output = tmp_path / "packaged"
    manifest = package_receiver_update(
        payload,
        output,
        generation=10,
        version="v1.0.10",
        compatibility=compatibility,
        private_key_path=key,
        registry=REGISTRY,
    )
    verified = verify_receiver_update(output, trust=trust, registry=REGISTRY)
    assert verified.manifest == manifest
    assert verified.signature["authority"] == "receiver-update"
    repeated = tmp_path / "packaged-repeated"
    package_receiver_update(
        payload,
        repeated,
        generation=10,
        version="v1.0.10",
        compatibility=compatibility,
        private_key_path=key,
        registry=REGISTRY,
    )
    for name in (MANIFEST_NAME, ARCHIVE_NAME, SIGNATURE_NAME):
        assert (output / name).read_bytes() == (repeated / name).read_bytes()


def _provision_active(store: ReceiverSlotStore, bundle: Path, manifest, clock: Clock):
    staged = store.stage(
        bundle,
        inventory=_inventory(),
        operation_id="receiver-install-0001",
        client_id="a" * 64,
    )
    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=lambda: None,
        readiness_probe=lambda: _readiness(manifest),
        wait_tick=clock.tick,
    )
    assert bootstrap.apply()["stage"] == "committed"
    return staged["candidate_slot"]


def test_installed_compatibility_inventory_uses_fixed_authenticated_protocols(tmp_path):
    _key, trust = _trust(tmp_path)
    root = tmp_path / "host"
    store = ReceiverSlotStore(root, trust=trust, registry=REGISTRY)
    atomic_document(
        store.bootstrap_protocol_path,
        {"schema": "iii.receiver-bootstrap-protocol/v1", "protocol": "1"},
    )
    atomic_document(
        store.cli_protocol_path,
        {"schema": "iii.receiver-cli-protocol/v1", "protocol": "1"},
    )
    inventory = store.inspect_compatibility_inventory()
    assert inventory.bootstrap_protocol == "1"
    assert inventory.cli_protocol == "1"
    assert inventory.request_protocol == "1"
    assert inventory.release_manifest_schema_versions == ()
    atomic_document(
        store.cli_protocol_path,
        {"schema": "iii.receiver-cli-protocol/v1", "protocol": "2"},
    )
    assert store.inspect_compatibility_inventory().cli_protocol == "2"


def test_signed_receiver_update_stages_only_inactive_slot_and_commits_handoff(tmp_path):
    key, trust = _trust(tmp_path)
    root = tmp_path / "host"
    store = ReceiverSlotStore(root, trust=trust, registry=REGISTRY)
    clock = Clock()
    first_bundle, first = _bundle(tmp_path, key, generation=1)
    first_slot = _provision_active(store, first_bundle, first, clock)
    assert first_slot == "a"
    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=lambda: None,
        readiness_probe=lambda: _readiness(second),
        wait_tick=clock.tick,
    )
    assert bootstrap.prepare_staging() == "b"
    second_bundle, second = _bundle(tmp_path, key, generation=2)
    staged = store.stage(
        second_bundle,
        inventory=_inventory(),
        operation_id="receiver-update-0002",
        client_id="a" * 64,
    )
    assert staged["candidate_slot"] == "b"
    assert store.active_slot() == "a"
    result = bootstrap.apply()
    assert result["stage"] == "committed"
    assert result["application_activation_started"] is False
    assert store.active_slot() == "b"
    assert store._selector_slot(store.fallback_path, required=True) == "a"
    bootstrap.assert_application_pair_compatible(
        release_manifest_schema_version="1",
        configuration_checkpoint_schema="iii.configuration-checkpoint/v1",
    )
    assert bootstrap.prepare_staging() == "a"
    third_bundle, third = _bundle(tmp_path, key, generation=3)
    third_state = store.stage(
        third_bundle,
        inventory=_inventory(),
        operation_id="receiver-update-0003",
        client_id="a" * 64,
    )
    assert third_state["candidate_receiver_id"] == third["receiver_id"]
    assert store.active_slot() == "b"
    assert store._selector_slot(store.fallback_path, required=True) == "b"


@pytest.mark.parametrize(
    "field",
    [
        "bootstrap_protocols",
        "cli_protocols",
        "request_protocols",
        "release_manifest_schema_versions",
        "journal_schemas",
        "audit_schemas",
        "activation_transaction_schemas",
        "activation_selector_schemas",
        "activation_health_transaction_schemas",
        "activation_health_evidence_schemas",
        "configuration_checkpoint_schemas",
    ],
)
def test_every_installed_compatibility_dimension_is_proved_before_switch(
    tmp_path, field
):
    key, trust = _trust(tmp_path)
    bundle, _manifest = _bundle(
        tmp_path,
        key,
        generation=2,
        compatibility_overrides={field: ["unsupported"]},
    )
    store = ReceiverSlotStore(tmp_path / "host", trust=trust, registry=REGISTRY)
    with pytest.raises(ContractError, match="compatibility proof failed"):
        store.stage(
            bundle,
            inventory=_inventory(),
            operation_id="receiver-update-0002",
            client_id="a" * 64,
        )
    assert store.active_slot() is None


def test_bad_signature_and_stable_host_payload_paths_are_rejected(tmp_path):
    key, trust = _trust(tmp_path)
    bundle, _manifest = _bundle(tmp_path, key, generation=1)
    signature = bytearray((bundle / SIGNATURE_NAME).read_bytes())
    signature[-3] = ord("A") if signature[-3] != ord("A") else ord("B")
    (bundle / SIGNATURE_NAME).write_bytes(signature)
    with pytest.raises(ContractError):
        verify_receiver_update(bundle, trust=trust, registry=REGISTRY)

    forbidden, _ = _bundle(
        tmp_path,
        key,
        generation=2,
        forbidden_file="bootstrap/replacement",
    )
    with pytest.raises(ContractError, match="stable host infrastructure"):
        verify_receiver_update(forbidden, trust=trust, registry=REGISTRY)


def test_receiver_slot_roots_cannot_be_redirected_by_symbolic_links(tmp_path):
    _key, trust = _trust(tmp_path)
    root = tmp_path / "host"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    receiver = root / "opt/iii/receiver"
    receiver.parent.mkdir(parents=True)
    receiver.symlink_to(redirected, target_is_directory=True)
    with pytest.raises(ContractError, match="receiver root cannot be a symbolic link"):
        ReceiverSlotStore(root, trust=trust, registry=REGISTRY)


def test_signed_archive_content_corruption_fails_before_inactive_slot_install(tmp_path):
    key, trust = _trust(tmp_path)
    bundle, _manifest = _bundle(tmp_path, key, generation=1)
    raw = (bundle / ARCHIVE_NAME).read_bytes()
    marker = b"receiver generation 1"
    assert marker in raw
    (bundle / ARCHIVE_NAME).write_bytes(
        raw.replace(marker, b"receiver generation 9", 1)
    )
    _resign_archive(bundle, key)
    store = ReceiverSlotStore(tmp_path / "host", trust=trust, registry=REGISTRY)
    with pytest.raises(ContractError, match="differs from signed content"):
        store.stage(
            bundle,
            inventory=_inventory(),
            operation_id="receiver-install-0001",
            client_id="a" * 64,
        )
    assert not (store.slots_root / "a").exists()


@pytest.mark.parametrize(
    "readiness_override",
    [
        {"socket_open": False},
        {"self_tests_passed": False},
        {"journal_compatible": False},
        {"generation": 999},
        {"receiver_id": "f" * 64},
        {"bootstrap_protocol": "2"},
    ],
)
def test_readiness_failure_or_timeout_restores_prior_receiver(
    tmp_path, readiness_override
):
    key, trust = _trust(tmp_path)
    root = tmp_path / "host"
    store = ReceiverSlotStore(root, trust=trust, registry=REGISTRY)
    clock = Clock()
    first_bundle, first = _bundle(tmp_path, key, generation=1)
    _provision_active(store, first_bundle, first, clock)
    second_bundle, second = _bundle(tmp_path, key, generation=2)
    probes = []
    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=lambda: None,
        readiness_probe=lambda: probes.append(True)
        or _readiness(second, **readiness_override),
        wait_tick=clock.tick,
    )
    bootstrap.prepare_staging()
    store.stage(
        second_bundle,
        inventory=_inventory(),
        operation_id="receiver-update-0002",
        client_id="a" * 64,
    )
    result = bootstrap.apply()
    assert result["stage"] == "reverted"
    assert result["application_activation_started"] is False
    assert store.active_slot() == "a"
    assert probes


def test_candidate_start_failure_restores_prior_receiver(tmp_path):
    key, trust = _trust(tmp_path)
    store = ReceiverSlotStore(tmp_path / "host", trust=trust, registry=REGISTRY)
    clock = Clock()
    first_bundle, first = _bundle(tmp_path, key, generation=1)
    _provision_active(store, first_bundle, first, clock)
    second_bundle, _second = _bundle(tmp_path, key, generation=2)
    calls = []

    def start():
        calls.append(True)
        raise OSError("exec failed")

    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=start,
        readiness_probe=lambda: {},
        wait_tick=clock.tick,
    )
    bootstrap.prepare_staging()
    store.stage(
        second_bundle,
        inventory=_inventory(),
        operation_id="receiver-update-0002",
        client_id="a" * 64,
    )
    result = bootstrap.apply()
    assert result["stage"] == "reverted"
    assert "failed to start" in result["failure"]
    assert store.active_slot() == "a"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stage",
    ["switch-prepared", "selector-switched", "candidate-started", "revert-prepared"],
)
def test_reboot_reconciles_each_persisted_switch_stage(stage, tmp_path):
    key, trust = _trust(tmp_path)
    root = tmp_path / "host"
    store = ReceiverSlotStore(root, trust=trust, registry=REGISTRY)
    clock = Clock()
    first_bundle, first = _bundle(tmp_path, key, generation=1)
    _provision_active(store, first_bundle, first, clock)
    second_bundle, second = _bundle(tmp_path, key, generation=2)
    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=lambda: None,
        readiness_probe=lambda: _readiness(second),
        wait_tick=clock.tick,
    )
    bootstrap.prepare_staging()
    store.stage(
        second_bundle,
        inventory=_inventory(),
        operation_id="receiver-update-0002",
        client_id="a" * 64,
    )
    state = bootstrap._load()
    if stage in {"selector-switched", "candidate-started"}:
        os.replace(store.current_path, store.current_path.parent / ".old-current")
        os.symlink(store.slots_root / "b", store.current_path)
        (store.current_path.parent / ".old-current").unlink()
    state["stage"] = stage
    state["state_id"] = content_identity(
        {key: value for key, value in state.items() if key != "state_id"}
    )
    atomic_document(store.state_path, state)
    clock.boot = "boot-b"
    if stage == "revert-prepared":
        result = bootstrap.reconcile()
        assert result["stage"] == "reverted"
        assert store.active_slot() == "a"
    else:
        result = bootstrap.reconcile()
        assert result["stage"] == "committed"
        assert store.active_slot() == "b"


def test_reconcile_is_idempotent_for_staged_and_terminal_states(tmp_path):
    key, trust = _trust(tmp_path)
    store = ReceiverSlotStore(tmp_path / "host", trust=trust, registry=REGISTRY)
    clock = Clock()
    bundle, manifest = _bundle(tmp_path, key, generation=1)
    store.stage(
        bundle,
        inventory=_inventory(),
        operation_id="receiver-install-0001",
        client_id="a" * 64,
    )
    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=lambda: None,
        readiness_probe=lambda: _readiness(manifest),
        wait_tick=clock.tick,
    )
    assert bootstrap.reconcile()["stage"] == "staged"
    assert bootstrap.apply()["stage"] == "committed"
    assert bootstrap.reconcile()["stage"] == "committed"


def test_application_rollback_does_not_revert_compatible_receiver(tmp_path):
    key, trust = _trust(tmp_path)
    root = tmp_path / "host"
    store = ReceiverSlotStore(root, trust=trust, registry=REGISTRY)
    clock = Clock()
    first_bundle, first = _bundle(tmp_path, key, generation=1)
    _provision_active(store, first_bundle, first, clock)
    second_bundle, second = _bundle(tmp_path, key, generation=2)
    bootstrap = ReceiverRecoveryBootstrap(
        store,
        monotonic=clock.monotonic,
        boot_id=clock.boot_id,
        restart_receiver=lambda: None,
        readiness_probe=lambda: _readiness(second),
        wait_tick=clock.tick,
    )
    bootstrap.prepare_staging()
    store.stage(
        second_bundle,
        inventory=_inventory(),
        operation_id="receiver-update-0002",
        client_id="a" * 64,
    )
    assert bootstrap.apply()["stage"] == "committed"
    bootstrap.assert_application_pair_compatible(
        release_manifest_schema_version="1",
        configuration_checkpoint_schema="iii.configuration-checkpoint/v1",
    )
    assert store.active_slot() == "b"
    with pytest.raises(ContractError, match="cannot manage restored release"):
        bootstrap.assert_application_pair_compatible(
            release_manifest_schema_version="2",
            configuration_checkpoint_schema="iii.configuration-checkpoint/v1",
        )
    assert store.active_slot() == "b"
