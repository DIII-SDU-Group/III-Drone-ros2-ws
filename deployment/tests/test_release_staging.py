from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import struct
from types import SimpleNamespace

import pytest

import iii_deployment.staging as staging_module
from iii_deployment.bundle import BundlePaths, load_bundle_limits, package_bundle_set
from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.release_status import create_status_index, create_status_statement
from iii_deployment.signers import add_trusted_signer, generate_signer, signer_proof
from iii_deployment.staging import ReleaseStore
from iii_deployment.contracts import canonical_json
from iii_deployment.receiver.access import AccessManager, client_id_for_public_key
from iii_deployment.receiver.engine import ReceiverEngine
from iii_deployment.receiver.protocol import Request
from iii_deployment.receiver.state import (
    AuditLog,
    OperationJournalStore,
    ReceiverControlStore,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
LIMITS = load_bundle_limits(ROOT / "deployment/operational-policy.json")
FIXTURE = ROOT / "deployment/tests/fixtures/release_manifest.json"
NOW = "2026-08-26T10:00:00Z"


class ImmediateExecutor:
    def submit(self, function, *args):
        function(*args)
        return SimpleNamespace()


@dataclass(frozen=True)
class BundleCase:
    release_id: str
    release_class: str
    version: str | None
    paths: BundlePaths


class ReleaseCases:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundle_store = root / "trust/bundles.json"
        self.status_store = root / "trust/status.json"
        self.bundle_keys: dict[str, tuple[Path, dict]] = {}
        for authority in ("ci-qualified", "workstation-field"):
            key = root / f"keys/{authority}.pem"
            public = root / f"keys/{authority}.public.json"
            descriptor = generate_signer(
                key,
                public,
                authority=authority,
                registry=REGISTRY,
            )
            add_trusted_signer(
                self.bundle_store,
                public,
                signer_proof(key),
                REGISTRY,
            )
            self.bundle_keys[authority] = (key, descriptor)
        self.status_key = root / "keys/status.pem"
        status_public = root / "keys/status.public.json"
        generate_signer(
            self.status_key,
            status_public,
            authority="release-status",
            registry=REGISTRY,
        )
        add_trusted_signer(
            self.status_store,
            status_public,
            signer_proof(self.status_key),
            REGISTRY,
        )
        self.statements: list[dict] = []

    def bundle(
        self,
        name: str,
        *,
        release_id: str,
        release_class: str,
        version: str | None,
    ) -> BundleCase:
        authority = (
            "ci-qualified" if release_class == "qualified" else "workstation-field"
        )
        key, descriptor = self.bundle_keys[authority]
        manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
        manifest["release_id"] = release_id
        manifest["release_class"] = release_class
        manifest["version"] = version
        manifest["signing"] = {
            "algorithm": "Ed25519",
            "signer_id": descriptor["signer_id"],
            "authority": authority,
        }
        if release_class == "field-development":
            manifest["source"]["branch"] = "deployment-infrastructure-redesign"
            manifest["qualification"].update(
                explicit_action=False,
                tag_on_release=False,
                tests_complete=False,
                evidence_complete=False,
            )
        case_root = self.root / "bundles" / name
        case_root.mkdir(parents=True)
        manifest_path = case_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        payloads: dict[str, Path] = {}
        for component in manifest["components"]:
            payload = case_root / f"{component}-payload"
            (payload / "share").mkdir(parents=True)
            (payload / "share/identity.txt").write_text(
                f"{release_id}:{component}\n", encoding="utf-8"
            )
            (payload / "bin").mkdir()
            executable = payload / "bin/iii-run"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            payloads[component] = payload
        output = case_root / "release-set"
        paths = package_bundle_set(
            manifest_path,
            payloads,
            key,
            output,
            registry=REGISTRY,
            host_limits=LIMITS,
        )
        return BundleCase(
            release_id,
            release_class,
            version,
            paths["drone"],
        )

    def append_status(self, case: BundleCase, status: str) -> dict:
        previous_global = self.statements[-1] if self.statements else None
        previous_release = next(
            (
                statement
                for statement in reversed(self.statements)
                if statement["release_id"] == case.release_id
            ),
            None,
        )
        statement = create_status_statement(
            operation_id=f"release-status-{len(self.statements) + 1:04d}",
            release_id=case.release_id,
            version=case.version or "v0.0.0",
            status=status,
            reason=f"test status {status}",
            superseding_version=None,
            recorded_at=NOW,
            private_key_path=self.status_key,
            registry=REGISTRY,
            previous_global=previous_global,
            previous_release=previous_release,
        )
        self.statements.append(statement)
        return self.status_index()

    def status_index(self) -> dict:
        return create_status_index(
            self.statements,
            generated_at=NOW,
            private_key_path=self.status_key,
            trusted_signers=self._status_trust(),
            registry=REGISTRY,
        )

    def _status_trust(self) -> dict:
        return json.loads(self.status_store.read_text(encoding="utf-8"))

    def store(self, target: Path, **kwargs) -> ReleaseStore:
        return ReleaseStore(
            target,
            bundle_trust=self.bundle_store,
            status_trust=self.status_store,
            registry=REGISTRY,
            host_limits=LIMITS,
            minimum_reserve_bytes=1,
            **kwargs,
        )


@pytest.fixture
def cases(tmp_path: Path) -> ReleaseCases:
    return ReleaseCases(tmp_path)


def _stage_qualified(
    cases: ReleaseCases,
    store: ReleaseStore,
    name: str,
    character: str,
    version: str,
) -> tuple[BundleCase, dict]:
    case = cases.bundle(
        name,
        release_id=character * 64,
        release_class="qualified",
        version=version,
    )
    index = cases.append_status(case, "qualified")
    store.stage(case.paths.directory, status_index=index, staged_at=NOW)
    return case, index


def _accept(
    store: ReleaseStore,
    case: BundleCase,
    index: dict | None,
    *,
    qualified: bool,
) -> dict:
    authorization = store.authorize_activation(
        case.release_id,
        status_index=index,
    )
    return store.record_acceptance(
        authorization,
        explicit_qualified_action=qualified,
    )


def _receiver_request(
    action: str,
    operation_id: str,
    client_id: str,
    payload: dict,
    nonce: str | None = None,
) -> Request:
    return Request.parse(
        canonical_json(
            {
                "protocol_version": "1",
                "action": action,
                "operation_id": operation_id,
                "client_id": client_id,
                "payload": payload,
                "nonce": nonce,
            }
        )
    )


def test_receiver_engine_stages_a_real_signed_bundle_and_status_chain(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    case = cases.bundle(
        "receiver-e2e",
        release_id="a" * 64,
        release_class="qualified",
        version="v1.0.0",
    )
    status_index = cases.append_status(case, "qualified")
    upload_id = "b" * 64
    upload = tmp_path / "incoming" / upload_id
    shutil.copytree(case.paths.directory, upload / "drone")
    (upload / "release-status-index.json").write_bytes(
        canonical_json(status_index) + b"\n"
    )
    operator_blob = (
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"o" * 32
    )
    operator_key = "ssh-ed25519 " + base64.b64encode(operator_blob).decode("ascii")
    operator_id = client_id_for_public_key(operator_key)
    access = AccessManager(
        state_path=tmp_path / "receiver/access.json",
        authorized_keys_path=tmp_path / "home/iii/.ssh/authorized_keys",
    )
    access.bootstrap([operator_key])
    clock = SimpleNamespace(value=10.0, boot="boot-a")
    control = ReceiverControlStore(
        tmp_path / "receiver",
        1,
        300,
        lambda: clock.value,
        lambda: clock.boot,
    )
    journals = OperationJournalStore(
        tmp_path / "receiver", lambda: clock.value, lambda: clock.boot
    )
    store = cases.store(tmp_path / "target")
    engine = ReceiverEngine(
        release_store=store,
        control=control,
        journals=journals,
        audit=AuditLog(
            tmp_path / "audit/receiver.jsonl", lambda: clock.value, lambda: clock.boot
        ),
        access=access,
        incoming_root=tmp_path / "incoming",
        receiver_root=tmp_path / "target/opt/iii/receiver",
        logical_target="drone",
        profile="real",
        live_state=lambda: {
            "active_release_id": None,
            "configuration_hash": "c" * 64,
            "commissioning_hash": "d" * 64,
            "profile": "real",
            "target_state_hash": "e" * 64,
        },
        executor=ImmediateExecutor(),
    )
    planned = engine.handle(
        _receiver_request(
            "plan-stage",
            "receiver-stage-0001",
            operator_id,
            {
                "artifact": {
                    "release_id": case.release_id,
                    "archive_sha256": hashlib.sha256(
                        case.paths.archive.read_bytes()
                    ).hexdigest(),
                    "upload_id": upload_id,
                    "status_index_id": status_index["index_id"],
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])
    engine.handle(
        _receiver_request(
            "stage",
            "receiver-stage-0001",
            operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    journal = journals.load("receiver-stage-0001")
    assert journal is not None and journal["state"] == "completed"
    assert journal["result"]["release_id"] == case.release_id
    state = store.state()
    assert state["candidate_release_id"] == case.release_id
    assert state["status_index_id"] == status_index["index_id"]


def test_first_stage_is_immutable_and_duplicate_is_idempotent(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    case, index = _stage_qualified(cases, store, "qualified", "a", "v1.0.0")
    release = tmp_path / "target/opt/iii/releases" / case.release_id
    assert (release / "share/identity.txt").read_text().startswith(case.release_id)
    assert not (release / "payload").exists()
    assert (release / "manifest.json").stat().st_mode & 0o222 == 0
    assert release.stat().st_mode & 0o222 == 0
    if os.geteuid() == 0:
        denied = subprocess.run(
            ["runuser", "-u", "nobody", "--", "touch", str(release / "untrusted")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert denied.returncode != 0
        assert not (release / "untrusted").exists()
    state = store.state()
    assert state["active_release_id"] is None
    assert state["candidate_release_id"] == case.release_id

    duplicate = store.stage(case.paths.directory, status_index=index, staged_at=NOW)
    assert duplicate.staged is False
    assert list((tmp_path / "target/opt/iii/releases").iterdir()) == [release]
    identity_file = release / "share/identity.txt"
    identity_file.chmod(0o640)
    identity_file.write_text("tampered\n", encoding="utf-8")
    identity_file.chmod(0o440)
    with pytest.raises(ContractError, match="differs from signed index"):
        store.stage(case.paths.directory, status_index=index, staged_at=NOW)


def test_low_disk_and_corrupt_or_interrupted_extraction_never_stage(
    tmp_path: Path,
    cases: ReleaseCases,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = cases.bundle(
        "low-disk",
        release_id="b" * 64,
        release_class="qualified",
        version="v1.0.1",
    )
    index = cases.append_status(case, "qualified")
    low_disk = cases.store(
        tmp_path / "low-target",
        minimum_reserve_percent=10,
        disk_usage=lambda _path: SimpleNamespace(total=1000, used=900, free=100),
    )
    with pytest.raises(ContractError, match="insufficient deployment storage"):
        low_disk.stage(case.paths.directory, status_index=index, staged_at=NOW)
    assert low_disk.state()["candidate_release_id"] is None
    assert not (tmp_path / "low-target/opt/iii/releases" / case.release_id).exists()

    interrupted = cases.store(tmp_path / "interrupted-target")

    def fail_extract(_source, destination, *_args, **_kwargs):
        destination.mkdir(parents=True)
        (destination / "partial").write_text("partial", encoding="utf-8")
        raise ContractError("injected extraction interruption")

    monkeypatch.setattr(staging_module, "extract_bundle", fail_extract)
    with pytest.raises(ContractError, match="injected extraction interruption"):
        interrupted.stage(case.paths.directory, status_index=index, staged_at=NOW)
    releases = tmp_path / "interrupted-target/opt/iii/releases"
    assert list(releases.iterdir()) == []
    assert interrupted.state()["candidate_release_id"] is None


def test_signature_tamper_and_corrupt_existing_slot_fail_closed(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    case = cases.bundle(
        "tamper",
        release_id="c" * 64,
        release_class="qualified",
        version="v1.0.2",
    )
    index = cases.append_status(case, "qualified")
    original = case.paths.archive.read_bytes()
    case.paths.archive.write_bytes(original + b"tamper")
    store = cases.store(tmp_path / "tamper-target")
    with pytest.raises(ContractError, match="checksum mismatch"):
        store.stage(case.paths.directory, status_index=index, staged_at=NOW)
    assert list((tmp_path / "tamper-target/opt/iii/releases").iterdir()) == []

    case.paths.archive.write_bytes(original)
    corrupt = tmp_path / "corrupt-target/opt/iii/releases" / case.release_id
    corrupt.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("{}\n", encoding="utf-8")
    corrupt_store = cases.store(tmp_path / "corrupt-target")
    with pytest.raises(
        ContractError,
        match="receipt identity is invalid|differs from the verified bundle identity",
    ):
        corrupt_store.stage(case.paths.directory, status_index=index, staged_at=NOW)
    assert (corrupt / "manifest.json").read_text() == "{}\n"


def test_retention_preserves_active_previous_field_and_qualified_anchor(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    qualified, index = _stage_qualified(cases, store, "anchor", "d", "v1.1.0")
    state = _accept(store, qualified, index, qualified=True)
    assert state["qualified_anchor_release_id"] == qualified.release_id

    fields: list[BundleCase] = []
    for offset, character in enumerate(("e", "f", "1"), start=1):
        field = cases.bundle(
            f"field-{offset}",
            release_id=character * 64,
            release_class="field-development",
            version=None,
        )
        store.stage(field.paths.directory, status_index=index, staged_at=NOW)
        _accept(store, field, index, qualified=False)
        fields.append(field)

    state = store.state()
    assert state["active_release_id"] == fields[2].release_id
    assert state["rollback_release_id"] == fields[1].release_id
    assert state["field_history"] == [fields[2].release_id, fields[1].release_id]
    assert state["qualified_anchor_release_id"] == qualified.release_id
    stale_file = (
        tmp_path
        / "target/opt/iii/releases"
        / fields[0].release_id
        / "share/identity.txt"
    )
    original = stale_file.read_bytes()
    stale_file.chmod(0o640)
    stale_file.write_text("tampered\n", encoding="utf-8")
    stale_file.chmod(0o440)
    with pytest.raises(ContractError, match="differs from signed index"):
        store.garbage_collect()
    assert (tmp_path / "target/opt/iii/releases" / fields[0].release_id).is_dir()
    stale_file.chmod(0o640)
    stale_file.write_bytes(original)
    stale_file.chmod(0o440)
    removed = store.garbage_collect()
    assert removed == [fields[0].release_id]
    for protected in (qualified, fields[1], fields[2]):
        assert (tmp_path / "target/opt/iii/releases" / protected.release_id).is_dir()


def test_only_explicit_qualified_acceptance_replaces_anchor(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    first, index = _stage_qualified(cases, store, "first", "2", "v2.0.0")
    _accept(store, first, index, qualified=True)
    second, index = _stage_qualified(cases, store, "second", "3", "v2.0.1")
    authorization = store.authorize_activation(second.release_id, status_index=index)
    with pytest.raises(ContractError, match="explicit qualified authority"):
        store.record_acceptance(
            authorization,
            explicit_qualified_action=False,
        )
    assert store.state()["qualified_anchor_release_id"] == first.release_id
    state = store.record_acceptance(
        authorization,
        explicit_qualified_action=True,
    )
    assert state["qualified_anchor_release_id"] == second.release_id
    assert state["rollback_release_id"] == first.release_id


def test_operator_rollback_authorization_is_state_bound_and_swaps_release_roles(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    first = cases.bundle(
        "rollback-first",
        release_id="a" * 64,
        release_class="field-development",
        version=None,
    )
    second = cases.bundle(
        "rollback-second",
        release_id="b" * 64,
        release_class="field-development",
        version=None,
    )
    store.stage(first.paths.directory, status_index=None, staged_at=NOW)
    _accept(store, first, None, qualified=False)
    store.stage(second.paths.directory, status_index=None, staged_at=NOW)
    _accept(store, second, None, qualified=False)

    authorization = store.authorize_rollback(first.release_id, status_index=None)
    assert authorization.release_id == first.release_id
    state = store.record_rollback_acceptance(authorization)
    assert state["active_release_id"] == first.release_id
    assert state["rollback_release_id"] == second.release_id
    assert state["candidate_release_id"] is None
    assert state["field_history"] == [first.release_id, second.release_id]
    with pytest.raises(ContractError, match="stale"):
        store.record_rollback_acceptance(authorization)


@pytest.mark.parametrize("status", ["withdrawn", "unsafe"])
def test_operator_rollback_rechecks_qualified_status_before_selection(
    tmp_path: Path,
    cases: ReleaseCases,
    status: str,
) -> None:
    store = cases.store(tmp_path / "target")
    previous, index = _stage_qualified(
        cases, store, "rollback-qualified", "c", "v2.1.0"
    )
    _accept(store, previous, index, qualified=True)
    active = cases.bundle(
        "rollback-active",
        release_id="d" * 64,
        release_class="field-development",
        version=None,
    )
    store.stage(active.paths.directory, status_index=index, staged_at=NOW)
    _accept(store, active, index, qualified=False)
    blocked_index = cases.append_status(previous, status)
    with pytest.raises(ContractError, match=status):
        store.authorize_rollback(
            previous.release_id,
            status_index=blocked_index,
        )
    state = store.state()
    assert state["active_release_id"] == active.release_id
    assert state["rollback_release_id"] == previous.release_id


def test_candidate_replacement_retains_only_one_unaccepted_slot(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    first = cases.bundle(
        "candidate-one",
        release_id="4" * 64,
        release_class="field-development",
        version=None,
    )
    second = cases.bundle(
        "candidate-two",
        release_id="5" * 64,
        release_class="field-development",
        version=None,
    )
    store.stage(first.paths.directory, status_index=None, staged_at=NOW)
    store.stage(second.paths.directory, status_index=None, staged_at=NOW)
    assert not (tmp_path / "target/opt/iii/releases" / first.release_id).exists()
    assert (tmp_path / "target/opt/iii/releases" / second.release_id).is_dir()
    assert store.state()["candidate_release_id"] == second.release_id


@pytest.mark.parametrize("status", ["withdrawn", "unsafe"])
def test_withdrawn_and_unsafe_candidates_cannot_be_newly_staged(
    tmp_path: Path,
    cases: ReleaseCases,
    status: str,
) -> None:
    case = cases.bundle(
        f"blocked-{status}",
        release_id=("6" if status == "withdrawn" else "7") * 64,
        release_class="qualified",
        version="v3.0.0" if status == "withdrawn" else "v3.0.1",
    )
    cases.append_status(case, "qualified")
    index = cases.append_status(case, status)
    store = cases.store(tmp_path / f"target-{status}")
    with pytest.raises(ContractError, match=status):
        store.stage(case.paths.directory, status_index=index, staged_at=NOW)
    assert store.state()["candidate_release_id"] is None
    assert not (store.releases_root / case.release_id).exists()


def test_unsafe_installed_state_is_monotonic_recovery_only_and_never_switches(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    case, qualified_index = _stage_qualified(cases, store, "unsafe", "8", "v4.0.0")
    _accept(store, case, qualified_index, qualified=True)
    unsafe_index = cases.append_status(case, "unsafe")
    state = store.refresh_status(unsafe_index)
    assert state["active_release_id"] == case.release_id
    assert state["qualified_anchor_release_id"] == case.release_id
    assert state["recovery"] == {
        "recovery_only": True,
        "flight_capable": False,
        "reason": (
            "unsafe installed release requires maintenance-safe recovery: "
            f"active={case.release_id}, qualified_anchor={case.release_id}"
        ),
    }
    assert (store.releases_root / case.release_id).is_dir()
    with pytest.raises(ContractError, match="stale release-status index"):
        store.refresh_status(qualified_index)
    assert store.state()["recovery"]["recovery_only"] is True


def test_unsafe_staged_release_requires_last_resort_recovery_authorization(
    tmp_path: Path,
    cases: ReleaseCases,
) -> None:
    store = cases.store(tmp_path / "target")
    case, _qualified_index = _stage_qualified(
        cases, store, "last-resort", "9", "v5.0.0"
    )
    unsafe_index = cases.append_status(case, "unsafe")
    store.refresh_status(unsafe_index)
    with pytest.raises(ContractError, match="last-resort recovery"):
        store.authorize_activation(case.release_id, status_index=unsafe_index)
    authorization = store.authorize_activation(
        case.release_id,
        status_index=unsafe_index,
        allow_unsafe_recovery=True,
    )
    assert authorization.recovery_only is True
    assert authorization.flight_capable is False
    state = store.record_acceptance(
        authorization,
        explicit_qualified_action=True,
    )
    assert state["active_release_id"] == case.release_id
    assert state["qualified_anchor_release_id"] is None
    assert state["recovery"]["recovery_only"] is True
