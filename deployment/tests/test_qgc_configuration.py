from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iii_deployment.contracts import canonical_json, content_identity
from iii_deployment.qgc_configuration import (
    QGCConfigurationError,
    QGCConfigurationStore,
    clean_exit_main,
)

ROOT = Path(__file__).resolve().parents[2]
RELEASE_ID = "a" * 64


def store(tmp_path: Path) -> QGCConfigurationStore:
    return QGCConfigurationStore(
        settings_path=tmp_path / "home/.config/QGroundControl.org/QGroundControl.ini",
        state_root=tmp_path / "state/qgc-config",
        policy_path=ROOT / "deployment/qgc/key-policy.json",
        baseline_path=ROOT / "deployment/qgc/managed-settings.json",
        schema_root=ROOT / "deployment/schemas/v1",
        now=lambda: "2026-08-27T06:00:00Z",
    )


def write_settings(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def legacy_settings() -> str:
    return """[General]
SettingsVersion=9
savePath=/home/iii/Documents/QGroundControl
virtualJoystick=true
mavlink2SigningKey=do-not-export

[FlightMapPosition]
Latitude=55.4700952
Longitude=10.3294067

[MainWindowState]
x=0
y=1637
width=1440
height=1243

[MAVLinkLogGroup]
Email=private@example.invalid
EnableAutoUpload=true
LogURL=https://logs.px4.io/upload
PublicLog=true
"""


def test_transactional_merge_backs_up_and_preserves_unowned_user_state(tmp_path):
    subject = store(tmp_path)
    write_settings(subject.settings_path, legacy_settings())

    result = subject.apply(
        qgc_version="5.0.8",
        release_id=RELEASE_ID,
        profile="real",
        qgc_running=False,
    )

    merged = subject.settings_path.read_text(encoding="utf-8")
    assert "savePath=/home/iii/Documents/QGroundControl" in merged
    assert "Latitude=55.4700952" in merged
    assert "Email=private@example.invalid" in merged
    assert "EnableAutoUpload=false" in merged
    assert "PublicLog=false" in merged
    assert "forwardMavlink=true" in merged
    assert "forwardMavlinkHostName=127.0.0.1:14551" in merged
    assert result["settings_id"] == subject.baseline["settings_id"]

    subject.restore(result["backup_id"])
    assert subject.settings_path.read_text(encoding="utf-8") == legacy_settings()


def test_merge_refuses_running_qgc_or_incompatible_inputs(tmp_path):
    subject = store(tmp_path)
    with pytest.raises(QGCConfigurationError, match="must be stopped"):
        subject.apply(
            qgc_version="5.0.8",
            release_id=RELEASE_ID,
            profile="sim",
            qgc_running=True,
        )
    with pytest.raises(QGCConfigurationError, match="incompatible"):
        subject.apply(
            qgc_version="4.4.4",
            release_id=RELEASE_ID,
            profile="sim",
            qgc_running=False,
        )


def test_new_settings_file_has_a_restorable_absence_backup(tmp_path):
    subject = store(tmp_path)
    result = subject.apply(
        qgc_version="5.0.8",
        release_id=RELEASE_ID,
        profile="sim",
        qgc_running=False,
    )
    assert subject.settings_path.is_file()
    subject.restore(result["backup_id"])
    assert not subject.settings_path.exists()


def test_malformed_settings_fail_without_overwriting_original(tmp_path):
    subject = store(tmp_path)
    malformed = b"[General]\nvalue=one\nvalue=two\n"
    subject.settings_path.parent.mkdir(parents=True, mode=0o700)
    subject.settings_path.write_bytes(malformed)
    with pytest.raises(QGCConfigurationError, match="malformed"):
        subject.apply(
            qgc_version="5.0.8",
            release_id=RELEASE_ID,
            profile="real",
            qgc_running=False,
        )
    assert subject.settings_path.read_bytes() == malformed


def test_capture_is_immutable_redacted_and_reports_unsafe_upload(tmp_path):
    subject = store(tmp_path)
    write_settings(subject.settings_path, legacy_settings())

    capture = subject.capture(
        qgc_version="5.0.8", release_id=RELEASE_ID, clean_exit=True
    )

    encoded = canonical_json(capture)
    assert b"do-not-export" not in encoded
    assert b"private@example.invalid" not in encoded
    assert b"/home/iii" not in encoded
    assert "General/mavlink2SigningKey" in capture["classification"]["sensitive"]
    assert "General/savePath" in capture["classification"]["local_preference"]
    assert {item["key"] for item in capture["violations"]} == {
        "MAVLinkLogGroup/EnableAutoUpload",
        "MAVLinkLogGroup/PublicLog",
    }
    assert subject.load_capture(capture["capture_id"]) == capture
    with pytest.raises(QGCConfigurationError, match="not clean and safe"):
        subject.promoted_baseline(
            capture["capture_id"], ["General/telemetrySaveNotArmed"]
        )


def test_explicit_capture_rejects_settings_changed_after_retained_plan(tmp_path):
    subject = store(tmp_path)
    write_settings(subject.settings_path, legacy_settings())
    retained_sha256 = hashlib.sha256(subject.settings_path.read_bytes()).hexdigest()
    write_settings(
        subject.settings_path,
        legacy_settings().replace("virtualJoystick=true", "virtualJoystick=false"),
    )

    with pytest.raises(QGCConfigurationError, match="changed after capture planning"):
        subject.capture(
            qgc_version="5.0.8",
            release_id=RELEASE_ID,
            clean_exit=False,
            expected_settings_sha256=retained_sha256,
        )
    assert list(subject.capture_root.glob("*.json")) == []


def test_reviewed_managed_key_promotion_never_mutates_runtime_settings(tmp_path):
    subject = store(tmp_path)
    subject.apply(
        qgc_version="5.0.8",
        release_id=RELEASE_ID,
        profile="real",
        qgc_running=False,
    )
    runtime_before = subject.settings_path.read_bytes()
    text = subject.settings_path.read_text(encoding="utf-8").replace(
        "telemetrySaveNotArmed=false", "telemetrySaveNotArmed=true"
    )
    write_settings(subject.settings_path, text)
    runtime_changed = subject.settings_path.read_bytes()
    capture = subject.capture(
        qgc_version="5.0.8", release_id=RELEASE_ID, clean_exit=True
    )
    diff = subject.diff(capture["capture_id"])
    assert diff["promotable"] is True
    assert [item["key"] for item in diff["changes"]] == [
        "General/telemetrySaveNotArmed"
    ]

    promoted = subject.promoted_baseline(
        capture["capture_id"], ["General/telemetrySaveNotArmed"]
    )
    assert promoted["settings"]["General/telemetrySaveNotArmed"] is True
    assert promoted["settings_id"] == content_identity(
        {key: value for key, value in promoted.items() if key != "settings_id"}
    )
    assert subject.settings_path.read_bytes() == runtime_changed
    assert subject.settings_path.read_bytes() != runtime_before
    with pytest.raises(QGCConfigurationError, match="changed managed keys"):
        subject.promoted_baseline(capture["capture_id"], ["General/savePath"])


def test_generated_param_cache_is_content_addressed_and_compatibility_bound(tmp_path):
    subject = store(tmp_path)
    generated = tmp_path / "QGroundControl.org/ParamCache"
    generated.mkdir(parents=True)
    (generated / "1_1.v2").write_bytes(b"generated-px4-parameter-metadata")
    manifest_id = "b" * 64

    record = subject.cache_generated(
        generated,
        qgc_version="5.0.8",
        px4_firmware="1.16.1",
        parameter_manifest_id=manifest_id,
    )
    assert (
        subject.verify_generated(
            record["cache_id"],
            qgc_version="5.0.8",
            px4_firmware="1.16.1",
            parameter_manifest_id=manifest_id,
        )
        == record
    )
    with pytest.raises(QGCConfigurationError, match="compatibility differs"):
        subject.verify_generated(
            record["cache_id"],
            qgc_version="5.0.8",
            px4_firmware="1.16.2",
            parameter_manifest_id=manifest_id,
        )
    cached = subject.generated_root / record["cache_id"] / "1_1.v2"
    cached.write_bytes(b"tampered")
    with pytest.raises(QGCConfigurationError, match="content changed"):
        subject.verify_generated(
            record["cache_id"],
            qgc_version="5.0.8",
            px4_firmware="1.16.1",
            parameter_manifest_id=manifest_id,
        )


def test_policy_and_baseline_identity_or_ownership_drift_fails_closed(tmp_path):
    policy = json.loads((ROOT / "deployment/qgc/key-policy.json").read_text())
    policy["classes"]["managed"]["exact"].append("General/newReleaseKey")
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(canonical_json(policy) + b"\n")
    with pytest.raises(QGCConfigurationError, match="policy identity mismatch"):
        QGCConfigurationStore(
            settings_path=tmp_path / "settings.ini",
            state_root=tmp_path / "state",
            policy_path=policy_path,
            baseline_path=ROOT / "deployment/qgc/managed-settings.json",
            schema_root=ROOT / "deployment/schemas/v1",
        )


def test_user_unit_clean_exit_entrypoint_uses_authenticated_active_release(
    tmp_path, capsys
):
    subject = store(tmp_path)
    subject.apply(
        qgc_version="5.0.8",
        release_id=RELEASE_ID,
        profile="real",
        qgc_running=False,
    )
    application = {
        "state_id": "0" * 64,
        "active_release_id": RELEASE_ID,
        "releases": {
            RELEASE_ID: {"qgroundcontrol": {"version": "5.0.8"}},
        },
    }
    application["state_id"] = content_identity(
        {key: value for key, value in application.items() if key != "state_id"}
    )
    application_path = tmp_path / "application-state.json"
    application_path.write_bytes(canonical_json(application) + b"\n")

    result = clean_exit_main(
        [
            "--application-state",
            str(application_path),
            "--settings",
            str(subject.settings_path),
            "--state-root",
            str(subject.state_root),
            "--policy",
            str(subject.policy_path),
            "--baseline",
            str(subject.baseline_path),
            "--schemas",
            str(ROOT / "deployment/schemas/v1"),
        ]
    )

    assert result == 0
    capture = json.loads(capsys.readouterr().out)
    assert capture["clean_exit"] is True
    assert capture["release_id"] == RELEASE_ID
    assert subject.load_capture(capture["capture_id"]) == capture
