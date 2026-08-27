from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii_deployment.bundle import load_bundle_limits, package_bundle_set
from iii_deployment.contracts import ContractRegistry, canonical_json, content_identity
from iii_deployment.gc_application import (
    GCApplicationError,
    GCApplicationStore,
    application_pair_compatible,
    compatibility_overlaps,
)
from iii_deployment.signers import add_trusted_signer, generate_signer, signer_proof

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "deployment/schemas/v1"
REGISTRY = ContractRegistry(SCHEMAS)
LIMITS = load_bundle_limits(ROOT / "deployment/operational-policy.json")
FIXTURE = ROOT / "deployment/tests/fixtures/release_manifest.json"
Usage = namedtuple("Usage", "total used free")


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return b"ok"


class CommandRunner:
    def __init__(self, digests: dict[str, str], *, qgc_running: bool = False):
        self.digests = digests
        self.qgc_running = qgc_running
        self.commands: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.commands.append(argv)
        if argv[:4] == ["systemctl", "--user", "is-active", "--quiet"]:
            return SimpleNamespace(
                returncode=0 if self.qgc_running else 3, stdout="", stderr=""
            )
        if len(argv) == 4 and argv[:2] == ["systemctl", "--user"]:
            if argv[3] == "iii-qgc.service" and argv[2] == "stop":
                self.qgc_running = False
            elif argv[3] == "iii-qgc.service" and argv[2] in {"start", "restart"}:
                self.qgc_running = True
        if argv[:2] == ["skopeo", "inspect"]:
            tag = argv[-1].removeprefix("docker-daemon:")
            return SimpleNamespace(
                returncode=0, stdout=self.digests[tag] + "\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _bundle(tmp_path: Path, *, release_id: str = "a" * 64, release_class="qualified"):
    key = tmp_path / "keys/release.pem"
    public = tmp_path / "keys/release.public.json"
    authority = "ci-qualified" if release_class == "qualified" else "workstation-field"
    signer = generate_signer(key, public, authority=authority, registry=REGISTRY)
    trust = tmp_path / "trust/trusted.json"
    add_trusted_signer(trust, public, signer_proof(key), REGISTRY)
    manifest = json.loads(FIXTURE.read_text())
    manifest["release_id"] = release_id
    manifest["release_class"] = release_class
    manifest["signing"] = {
        "algorithm": "Ed25519",
        "signer_id": signer["signer_id"],
        "authority": authority,
    }
    qgc_bytes = b"pinned-qgroundcontrol-appimage\n"
    qgc_sha = hashlib.sha256(qgc_bytes).hexdigest()
    qgc_policy_bytes = (ROOT / "deployment/qgc/key-policy.json").read_bytes()
    qgc_baseline_bytes = (ROOT / "deployment/qgc/managed-settings.json").read_bytes()
    qgc_policy = json.loads(qgc_policy_bytes)
    qgc_baseline = json.loads(qgc_baseline_bytes)
    manifest["qgc"] = {
        "managed_settings_sha256": hashlib.sha256(qgc_baseline_bytes).hexdigest(),
        "compatible_versions": ["5.0.8"],
        "selected_version": "5.0.8",
        "appimage_sha256": qgc_sha,
        "update_owner": "iii-gc-release",
    }
    if release_class == "field-development":
        manifest["version"] = None
        manifest["source"]["branch"] = "deployment-infrastructure-redesign"
        manifest["qualification"] = {
            **manifest["qualification"],
            "explicit_action": False,
            "tag_on_release": False,
            "tests_complete": False,
            "evidence_complete": False,
        }
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest))
    drone = tmp_path / "drone"
    drone.mkdir()
    (drone / "payload").write_text("drone\n")
    gc = tmp_path / "gc"
    (gc / "images").mkdir(parents=True)
    (gc / "qgc/config").mkdir(parents=True)
    (gc / "qgc/QGroundControl.AppImage").write_bytes(qgc_bytes)
    (gc / "qgc/config/key-policy.json").write_bytes(qgc_policy_bytes)
    (gc / "qgc/config/managed-settings.json").write_bytes(qgc_baseline_bytes)
    (gc / "compose.yml").write_text("services: {}\n")
    images = []
    digests = {}
    for index, name in enumerate(("frontend", "proxy"), start=2):
        archive = gc / f"images/{name}.oci"
        archive.write_bytes((name + "-oci\n").encode())
        manifest_digest = "sha256:" + str(index) * 64
        tag = f"iii-drone-gc-{name}:{release_id}"
        digests[tag] = manifest_digest
        images.append(
            {
                "name": name,
                "archive": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "bytes": archive.stat().st_size,
                "manifest_digest": manifest_digest,
                "base_images": [f"example.invalid/{name}@sha256:" + "4" * 64],
                "smoke_test": "passed",
            }
        )
    body = {
        "schema": "iii.gc-build-record/v1",
        "source_identity": "5" * 64,
        "source_commit": "6" * 40,
        "version": "v1.2.3",
        "platform": {"os": "linux", "architecture": "amd64"},
        "inputs_sha256": "7" * 64,
        "test_record_sha256": "8" * 64,
        "images": images,
        "qgroundcontrol": {
            "version": "5.0.8",
            "appimage": "QGroundControl.AppImage",
            "source_url": "https://github.com/mavlink/qgroundcontrol/releases/download/v5.0.8/QGroundControl-x86_64.AppImage",
            "sha256": qgc_sha,
            "bytes": len(qgc_bytes),
            "appimage_update_information": "",
            "update_owner": "iii-gc-release",
            "runtime_self_check": "passed",
            "configuration": {
                "policy": "qgc/config/key-policy.json",
                "policy_sha256": hashlib.sha256(qgc_policy_bytes).hexdigest(),
                "policy_id": qgc_policy["policy_id"],
                "baseline": "qgc/config/managed-settings.json",
                "baseline_sha256": hashlib.sha256(qgc_baseline_bytes).hexdigest(),
                "settings_id": qgc_baseline["settings_id"],
            },
        },
        "application": {
            "compose": "compose.yml",
            "compose_sha256": hashlib.sha256(
                (gc / "compose.yml").read_bytes()
            ).hexdigest(),
            "environment": "application.env",
        },
        "complete": True,
    }
    record = {"build_id": content_identity(body), **body}
    (gc / "build-record.json").write_bytes(canonical_json(record) + b"\n")
    output = tmp_path / "release-set"
    paths = package_bundle_set(
        manifest_path,
        {"drone": drone, "gc": gc},
        key,
        output,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    return paths["gc"].directory, trust, digests, manifest


def _store(tmp_path: Path, trust: Path, runner, **kwargs) -> GCApplicationStore:
    return GCApplicationStore(
        application_root=tmp_path / "home/.local/share/iii/gc-applications",
        state_root=tmp_path / "home/.local/state/iii/gc",
        cache_root=tmp_path / "home/.cache/iii",
        policy_path=ROOT / "deployment/gc-application-policy.json",
        schema_root=SCHEMAS,
        trusted_signers=trust,
        operational_policy_path=ROOT / "deployment/operational-policy.json",
        runner=runner,
        health_opener=lambda *_args, **_kwargs: Response(),
        disk_usage_provider=lambda _path: Usage(
            200 * 1024**3, 20 * 1024**3, 180 * 1024**3
        ),
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        **kwargs,
    )


def _safe_real() -> dict:
    return {
        "connected": True,
        "profile": "real",
        "runtime_api_available": True,
        "runtime_identity_matches": True,
        "runtime_fresh": True,
        "px4_available": True,
        "px4_fresh": True,
        "armed": False,
        "in_air": False,
        "mission_fresh": True,
        "mission_active": False,
        "mission_control_owner": False,
        "operation_fresh": True,
        "custom_operation_active": False,
        "custom_operation_control_owner": False,
        "direct_operation_active": False,
        "reference_owner_active": False,
        "continuously_safe_for_s": 3.5,
    }


def test_offline_stage_and_activate_select_exact_gc_and_qgc_slots(tmp_path: Path):
    component, trust, digests, _manifest = _bundle(tmp_path)
    runner = CommandRunner(digests, qgc_running=True)
    settings = tmp_path / "home/.config/QGroundControl.org/QGroundControl.ini"
    logs = tmp_path / "home/Documents/QGroundControl/flight.tlog"
    settings.parent.mkdir(parents=True)
    logs.parent.mkdir(parents=True)
    settings.write_text("[General]\nappFontPointSize=14\nforwardMavlink=false\n")
    logs.write_text("log\n")
    store = _store(tmp_path, trust, runner, qgc_settings_path=settings)

    staged = store.stage(component)
    activated = store.activate(
        staged["release_id"], operation_id="gc-update-1", safety={"connected": False}
    )

    state = store.state()
    assert state["active_release_id"] == "a" * 64
    assert state["qualified_anchor_release_id"] == "a" * 64
    assert state["staged_release_id"] is None
    assert (store.application_root / "current").resolve() == store.releases_root / (
        "a" * 64
    )
    assert (store.application_root / "qgc/current/QGroundControl.AppImage").is_file()
    assert activated["qgc_sha256"] == state["active_qgc_sha256"]
    merged = settings.read_text()
    assert "appFontPointSize=14" in merged
    assert "forwardMavlink=true" in merged
    assert "EnableAutoUpload=false" in merged
    assert logs.read_text() == "log\n"
    assert [command[:2] for command in runner.commands].count(["skopeo", "copy"]) == 4
    assert ["systemctl", "--user", "restart", "iii-gc.target"] in runner.commands
    assert ["systemctl", "--user", "stop", "iii-qgc.service"] in runner.commands
    assert ["systemctl", "--user", "start", "iii-qgc.service"] in runner.commands
    backup_id = activated["qgc_configuration"]["backup_id"]
    backup = store.qgc_configuration_state_root / "backups" / f"{backup_id}.ini"
    assert backup.read_text() == (
        "[General]\nappFontPointSize=14\nforwardMavlink=false\n"
    )
    cache_entry = json.loads(
        next((store.cache_root / "artifact-index").glob("*.json")).read_text()
    )
    assert set(cache_entry["protected_domains"]) == {"active", "qualified-anchor"}


def test_connected_real_update_rejects_armed_state_and_override_cannot_waive_it(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    release_id = store.stage(component)["release_id"]
    armed = {**_safe_real(), "armed": True}
    warning = store.policy["safety"]["override_warning"]

    with pytest.raises(GCApplicationError, match="armed"):
        store.activate(release_id, operation_id="unsafe-normal", safety=armed)
    with pytest.raises(GCApplicationError, match="armed"):
        store.activate(
            release_id,
            operation_id="unsafe-override",
            safety=armed,
            override_reason="recover operator surface after failed update",
            override_confirmation=warning,
        )
    assert not (store.control_root / "drain.json").exists()
    assert store.state()["active_release_id"] is None


def test_unknown_real_state_requires_separately_confirmed_audited_override(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    release_id = store.stage(component)["release_id"]
    unknown = {"connected": True, "profile": "real"}
    with pytest.raises(GCApplicationError, match="lacks fresh"):
        store.activate(release_id, operation_id="unknown", safety=unknown)

    result = store.activate(
        release_id,
        operation_id="recovery",
        safety=unknown,
        override_reason="restore a failed local operator application",
        override_confirmation=store.policy["safety"]["override_warning"],
    )
    assert result["override_id"]
    audit = [json.loads(line) for line in store.audit_path.read_text().splitlines()]
    assert any(item["event"] == "maintenance-override" for item in audit)
    assert audit[-1]["previous_audit_id"] == audit[-2]["audit_id"]


def test_reconcile_after_abrupt_selector_interruption_restores_recorded_pair(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    runner = CommandRunner(digests)

    def interrupt(phase: str):
        if phase == "selected":
            raise KeyboardInterrupt

    store = _store(tmp_path, trust, runner, failpoint=interrupt)
    release_id = store.stage(component)["release_id"]
    with pytest.raises(KeyboardInterrupt):
        store.activate(
            release_id, operation_id="interrupted", safety={"connected": False}
        )
    assert store.journal_path.is_file()
    assert (store.application_root / "current").is_symlink()

    recovered = _store(tmp_path, trust, runner).reconcile()
    assert recovered["state"] == "rolled-back"
    assert not (store.application_root / "current").exists()
    assert not store.journal_path.exists()
    assert store.state()["active_release_id"] is None


def test_activation_failure_after_qgc_merge_restores_exact_bytes_and_service(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    runner = CommandRunner(digests, qgc_running=True)
    settings = tmp_path / "home/.config/QGroundControl.org/QGroundControl.ini"
    settings.parent.mkdir(parents=True)
    original = (
        b"[General]\nappFontPointSize=15\nforwardMavlink=false\n\n[Vendor]\nopaque=x\n"
    )
    settings.write_bytes(original)

    def fail(phase: str):
        if phase == "qgc-configured":
            raise GCApplicationError("injected post-merge failure")

    store = _store(
        tmp_path,
        trust,
        runner,
        qgc_settings_path=settings,
        failpoint=fail,
    )
    release_id = store.stage(component)["release_id"]
    with pytest.raises(GCApplicationError, match="post-merge"):
        store.activate(
            release_id,
            operation_id="qgc-merge-failure",
            safety={"connected": False},
        )

    assert settings.read_bytes() == original
    assert runner.qgc_running is True
    assert not store.journal_path.exists()
    assert ["systemctl", "--user", "stop", "iii-qgc.service"] in runner.commands
    assert ["systemctl", "--user", "start", "iii-qgc.service"] in runner.commands


def test_reconcile_restores_qgc_bytes_and_exact_prior_service_state(tmp_path: Path):
    component, trust, digests, _manifest = _bundle(tmp_path)
    runner = CommandRunner(digests, qgc_running=True)
    settings = tmp_path / "home/.config/QGroundControl.org/QGroundControl.ini"
    settings.parent.mkdir(parents=True)
    original = b"[General]\nforwardMavlink=false\n\n[Vendor]\npreserve=byte-for-byte\n"
    settings.write_bytes(original)

    def interrupt(phase: str):
        if phase == "selected":
            raise KeyboardInterrupt("simulated power loss")

    store = _store(
        tmp_path,
        trust,
        runner,
        qgc_settings_path=settings,
        failpoint=interrupt,
    )
    release_id = store.stage(component)["release_id"]
    with pytest.raises(KeyboardInterrupt, match="power loss"):
        store.activate(
            release_id,
            operation_id="qgc-power-loss",
            safety={"connected": False},
        )
    journal = json.loads(store.journal_path.read_text())
    assert journal["qgc_config_backup_id"]
    assert journal["qgc_was_running"] is True
    assert runner.qgc_running is False
    assert settings.read_bytes() != original

    recovered = _store(tmp_path, trust, runner, qgc_settings_path=settings).reconcile()
    assert recovered["state"] == "rolled-back"
    assert settings.read_bytes() == original
    assert runner.qgc_running is True
    assert not store.journal_path.exists()


def test_reconcile_after_state_commit_interruption_restores_exact_previous_state(
    tmp_path: Path, monkeypatch
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    runner = CommandRunner(digests)
    store = _store(tmp_path, trust, runner)
    release_id = store.stage(component)["release_id"]
    previous_state = store.state()
    commit_state = store._commit_state

    def interrupt_after_commit(value):
        commit_state(value)
        if value.get("active_release_id") == release_id:
            raise KeyboardInterrupt("power loss after durable state commit")

    monkeypatch.setattr(store, "_commit_state", interrupt_after_commit)
    with pytest.raises(KeyboardInterrupt, match="power loss"):
        store.activate(
            release_id,
            operation_id="post-commit-interruption",
            safety={"connected": False},
        )

    assert store.journal_path.is_file()
    assert store.state()["active_release_id"] == release_id
    recovered_store = _store(tmp_path, trust, runner)
    recovered = recovered_store.reconcile()
    restored = recovered_store.state()
    assert recovered["state"] == "rolled-back"
    assert restored["active_release_id"] == previous_state["active_release_id"]
    assert restored["active_qgc_sha256"] == previous_state["active_qgc_sha256"]
    assert restored["staged_release_id"] == previous_state["staged_release_id"]
    assert restored["releases"] == previous_state["releases"]
    assert restored["generation"] >= previous_state["generation"]
    assert not recovered_store.journal_path.exists()
    assert not (recovered_store.application_root / "current").exists()


def test_cache_pressure_never_evicts_offline_or_selected_domains(tmp_path: Path):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    artifacts = store.cache_root / "artifacts"
    for index, domains in enumerate(([], ["offline"], ["active"]), start=1):
        entry = artifacts / (str(index) * 64)
        entry.mkdir(parents=True)
        (entry / "payload").write_bytes(b"x" * index)
        value = {
            "schema": "iii.gc-artifact-cache-entry/v1",
            "archive_sha256": str(index) * 64,
            "bytes": 20 * 1024**3,
            "last_used_at": f"2026-08-2{index}T00:00:00+00:00",
            "protected_domains": domains,
        }
        index_root = store.cache_root / "artifact-index"
        index_root.mkdir(parents=True, exist_ok=True)
        (index_root / f"{str(index) * 64}.json").write_bytes(
            canonical_json(value) + b"\n"
        )
    store.policy["cache"]["non_protected_quota_bytes"] = 10 * 1024**3
    report = store.prune_cache()
    assert report["removed"] == ["1" * 64]
    assert not (artifacts / ("1" * 64)).exists()
    assert (artifacts / ("2" * 64)).is_dir()
    assert (artifacts / ("3" * 64)).is_dir()


def test_offline_stage_marks_bundle_as_non_evictable_and_preserves_that_domain(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))

    staged = store.stage(component, protect_offline=True)
    entry_path = next((store.cache_root / "artifact-index").glob("*.json"))
    staged_entry = json.loads(entry_path.read_text())

    assert staged["offline_protected"] is True
    assert set(staged_entry["protected_domains"]) == {
        "offline",
        "staged-candidate",
    }

    store.policy["cache"]["non_protected_quota_bytes"] = 0
    report = store.prune_cache()
    preserved_entry = json.loads(entry_path.read_text())
    assert report["removed"] == []
    assert set(preserved_entry["protected_domains"]) == {
        "offline",
        "staged-candidate",
    }


def test_stage_recovers_durable_unindexed_cache_and_stale_partial(tmp_path: Path):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    first = store.stage(component)
    index_path = next((store.cache_root / "artifact-index").glob("*.json"))
    index_path.unlink()
    stale = store.cache_root / "artifacts/.dead.partial-999"
    stale.mkdir()
    (stale / "incomplete").write_text("interrupted\n")

    second = store.stage(component, protect_offline=True)

    assert second["release_id"] == first["release_id"]
    assert not stale.exists()
    recovered = json.loads(index_path.read_text())
    assert set(recovered["protected_domains"]) == {
        "offline",
        "staged-candidate",
    }


def test_activation_reauthenticates_signed_payload_and_rejects_slot_tamper(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    release_id = store.stage(component)["release_id"]
    (store.releases_root / release_id / "payload/compose.yml").write_text(
        "services: {tampered: {}}\n"
    )

    with pytest.raises(GCApplicationError, match="payload bytes differ"):
        store.activate(
            release_id,
            operation_id="tampered-slot",
            safety={"connected": False},
        )

    assert store.state()["active_release_id"] is None
    assert not (store.control_root / "drain.json").exists()


def test_post_commit_cleanup_failure_does_not_undo_healthy_activation(
    tmp_path: Path, monkeypatch
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    release_id = store.stage(component)["release_id"]

    def fail_cleanup():
        raise GCApplicationError("injected cleanup failure")

    monkeypatch.setattr(store, "_garbage_collect_locked", fail_cleanup)
    activated = store.activate(
        release_id,
        operation_id="cleanup-deferred",
        safety={"connected": False},
    )

    assert activated["state"] == "active"
    assert activated["cleanup"]["state"] == "deferred"
    assert store.state()["active_release_id"] == release_id
    assert not store.journal_path.exists()
    audit = [json.loads(line) for line in store.audit_path.read_text().splitlines()]
    assert audit[-1]["event"] == "garbage-collect"
    assert audit[-1]["outcome"] == "deferred"


def test_state_and_cache_semantics_reject_ambiguous_or_unknown_content(
    tmp_path: Path,
):
    component, trust, digests, _manifest = _bundle(tmp_path)
    store = _store(tmp_path, trust, CommandRunner(digests))
    store.stage(component)
    state = store.state()
    release = state["releases"][state["staged_release_id"]]
    release["images"][1] = dict(release["images"][0])
    state["state_id"] = content_identity(
        {key: value for key, value in state.items() if key != "state_id"}
    )
    with pytest.raises(GCApplicationError, match="image state"):
        store._validate_state(state)

    cache_path = next((store.cache_root / "artifact-index").glob("*.json"))
    cache = json.loads(cache_path.read_text())
    cache["protected_domains"] = ["operator-typo"]
    cache_path.write_bytes(canonical_json(cache) + b"\n")
    with pytest.raises(GCApplicationError, match="cache entry contract"):
        store.prune_cache()


def test_pair_compatibility_uses_declared_api_and_schema_intersections():
    manifest = json.loads(FIXTURE.read_text())
    manifest["qgc"].update(selected_version="5.0.6")
    assert compatibility_overlaps(">=2.0.0,<3.0.0", ">=2.5.0,<4.0.0")
    assert not compatibility_overlaps(">=2.0.0,<3.0.0", ">=3.0.0,<4.0.0")
    assert application_pair_compatible(manifest, manifest)
    incompatible = json.loads(json.dumps(manifest))
    incompatible["compatibility"]["api_ranges"]["runtime_api"] = ">=3.0.0,<4.0.0"
    assert not application_pair_compatible(manifest, incompatible)
