from __future__ import annotations

import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from iii_deployment.contracts import canonical_json, content_identity
from iii_deployment.px4_parameters import (
    PX4ApplyError,
    PX4ParameterError,
    PX4ParameterMonitor,
    PX4ParameterStore,
)

ROOT = Path(__file__).resolve().parents[2]


def parameter(
    name: str,
    value,
    classification: str = "operator-tunable",
    mav_type: str = "INT32",
):
    preserve = classification == "calibration-identity"
    return {
        "name": name,
        "mav_type": mav_type,
        "value": None if preserve else value,
        "classification": classification,
        "enforcement": "preserve" if preserve else "exact",
        "tolerance": 1e-6 if mav_type == "REAL32" else 0,
    }


def manifest(profile: str) -> dict:
    values = [
        parameter("CAL_ACC0_ID", None, "calibration-identity"),
        parameter("COM_RC_IN_MODE", 1, "release-required"),
        parameter("MPC_XY_VEL_MAX", 12.0, mav_type="REAL32"),
        parameter("NAV_RCL_ACT", 0, "release-required"),
    ]
    result = {
        "schema": "iii.px4-parameter-manifest/v1",
        "manifest_id": "0" * 64,
        "profile": profile,
        "network_baseline_id": "3" * 64 if profile == "real" else None,
        "firmware": {
            "family": "PX4",
            "compatible_range": ">=1.16.1,<1.17.0",
            "reference_version": "1.16.1",
            "reference_commit": "1" * 40,
        },
        "inventory": {
            "complete": True,
            "parameter_count": len(values),
            "source": "px4-sitl-reference",
            "source_sha256": "2" * 64,
        },
        "parameters": values,
    }
    result["manifest_id"] = content_identity(
        {key: value for key, value in result.items() if key != "manifest_id"}
    )
    return result


class FakeAdapter:
    def __init__(self):
        self.connected = True
        self.armed = False
        self.values = {
            "CAL_ACC0_ID": (1234, "INT32"),
            "COM_RC_IN_MODE": (1, "INT32"),
            "MPC_XY_VEL_MAX": (10.0, "REAL32"),
            "NAV_RCL_ACT": (0, "INT32"),
        }
        self.partial = False
        self.write_calls = []
        self.pull_calls = 0
        self.fail_once = None
        self.mismatch_once = None

    def status(self):
        return {
            "connected": self.connected,
            "armed": self.armed,
            "system_id": 1,
            "component_id": 1,
            "firmware_version": "1.16.1",
            "firmware_commit": "1" * 10,
        }

    def pull_all(self):
        self.pull_calls += 1
        items = sorted(self.values.items())
        if self.partial:
            items = items[:-1]
        count = len(self.values)
        return [
            {
                "name": name,
                "value": value,
                "mav_type": mav_type,
                "index": index,
                "count": count,
            }
            for index, (name, (value, mav_type)) in enumerate(items)
        ]

    def write(self, name, value, mav_type):
        self.write_calls.append((name, value, mav_type))
        self.values[name] = (value, mav_type)
        if self.fail_once == name:
            self.fail_once = None
            raise PX4ParameterError("injected interrupted write")
        observed = value
        if self.mismatch_once == name:
            self.mismatch_once = None
            observed = value + 1
        return {"name": name, "value": observed, "mav_type": mav_type}

    def wait_parameter_event(self, timeout):
        return False


def make_store(tmp_path: Path, adapter: FakeAdapter | None = None):
    paths = {}
    for profile in ("real", "sim"):
        path = tmp_path / f"manifests/{profile}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json(manifest(profile)) + b"\n")
        paths[profile] = path
    selected = adapter or FakeAdapter()
    return (
        PX4ParameterStore(
            manifest_paths=paths,
            state_root=tmp_path / "state",
            schema_root=ROOT / "deployment/schemas/v1",
            adapter=selected,
            now=lambda: "2026-08-27T07:00:00Z",
        ),
        selected,
    )


def test_full_pull_classifies_required_tunable_and_preserved_drift(tmp_path):
    subject, adapter = make_store(tmp_path)
    snapshot = subject.pull("real")
    comparison = subject.compare("real", snapshot["snapshot_id"])
    assert snapshot["parameter_count"] == 4
    assert comparison["inventory_complete"] is True
    assert comparison["required_match"] is True
    assert [item["name"] for item in comparison["drift"]["operator-tunable"]] == [
        "MPC_XY_VEL_MAX"
    ]
    assert comparison["preserved_calibration_identity"] == ["CAL_ACC0_ID"]
    assert adapter.write_calls == []


def test_pull_reuses_audit_status_without_second_firmware_request(tmp_path):
    subject, adapter = make_store(tmp_path)
    observed = adapter.status()

    def repeated_status():
        raise PX4ParameterError("PX4 firmware identity timed out")

    adapter.status = repeated_status
    snapshot = subject.pull("sim", status=observed)
    assert snapshot["parameter_count"] == 4
    assert adapter.pull_calls == 1


def test_real_activation_health_fails_required_drift_without_writing(tmp_path):
    subject, adapter = make_store(tmp_path)
    adapter.values["COM_RC_IN_MODE"] = (4, "INT32")
    health = subject.activation_health("real")
    assert health["healthy"] is False
    assert health["parameter_manifest_matches"] is False
    assert health["writes_performed"] == 0
    assert adapter.write_calls == []


def test_activation_evidence_binds_release_full_snapshot_and_no_write(tmp_path):
    subject, adapter = make_store(tmp_path)
    evidence = subject.activation_evidence("real", release_id="a" * 64)
    assert evidence["release_id"] == "a" * 64
    assert evidence["manifest_id"] == subject.manifest("real")["manifest_id"]
    assert evidence["snapshot"]["parameter_count"] == 4
    assert evidence["comparison"]["inventory_complete"] is True
    assert evidence["comparison"]["required_match"] is True
    assert evidence["writes_performed"] == 0
    assert adapter.write_calls == []


def test_activation_evidence_retains_current_qgc_provenance_after_prior_pull(tmp_path):
    subject, _ = make_store(tmp_path)
    prior = subject.pull("real")
    assert prior["provenance"] == "mavlink-complete-inventory"

    evidence = subject.activation_evidence("real", release_id="a" * 64)

    assert evidence["snapshot"]["snapshot_id"] == prior["snapshot_id"]
    assert evidence["snapshot"]["provenance"] == "qgc-forwarded-mavlink-observation"
    from iii_deployment.receiver.protocol import validate_px4_activation_evidence

    validate_px4_activation_evidence(evidence)


def test_partial_inventory_and_armed_bulk_transfer_fail_closed(tmp_path):
    subject, adapter = make_store(tmp_path)
    adapter.partial = True
    with pytest.raises(PX4ParameterError, match="partial or duplicated"):
        subject.pull("sim")
    adapter.partial = False
    adapter.armed = True
    pulls = adapter.pull_calls
    with pytest.raises(PX4ParameterError, match="forbidden while armed"):
        subject.pull("sim")
    assert adapter.pull_calls == pulls


def test_firmware_commit_must_match_release_reference_before_inventory(tmp_path):
    subject, adapter = make_store(tmp_path)
    original_status = adapter.status

    def wrong_commit():
        return {**original_status(), "firmware_commit": "d" * 10}

    adapter.status = wrong_commit
    with pytest.raises(PX4ParameterError, match="firmware commit differs"):
        subject.pull("real")
    assert adapter.pull_calls == 0


def test_px4_autopilot_version_recovers_five_byte_git_prefix_after_vendor_overlay():
    # PX4 masks 0x7f41496535c54924 to five Git bytes in a little-endian
    # uint64, then overwrites the low three bytes with its vendor version.
    transmitted = bytes.fromhex("000000356549417f")
    from iii_deployment.px4_parameters import MavlinkParameterAdapter

    assert MavlinkParameterAdapter._decode_firmware_commit(transmitted) == "7f41496535"


def test_mavlink_discovery_ignores_payload_heartbeat_before_px4(monkeypatch):
    from types import SimpleNamespace
    from iii_deployment.px4_parameters import MavlinkParameterAdapter

    payload = SimpleNamespace(autopilot=0)
    px4 = SimpleNamespace(autopilot=12)
    messages = [payload, px4]
    adapter = object.__new__(MavlinkParameterAdapter)
    adapter.timeout = 1.0
    adapter.mavutil = SimpleNamespace(
        mavlink=SimpleNamespace(MAV_AUTOPILOT_PX4=12)
    )
    adapter.connection = SimpleNamespace(
        recv_match=lambda **_kwargs: messages.pop(0) if messages else None
    )
    monkeypatch.setattr("iii_deployment.px4_parameters.time.monotonic", lambda: 0.0)

    assert adapter._heartbeat() is px4


def test_mavlink_full_inventory_ignores_other_vehicle_and_restarts_mixed_generation():
    from iii_deployment.px4_parameters import MavlinkParameterAdapter

    def value(name, index, count, *, system=8):
        return SimpleNamespace(
            param_id=name,
            param_type=6,
            param_value=0.0,
            param_index=index,
            param_count=count,
            get_srcSystem=lambda: system,
            get_srcComponent=lambda: 1,
        )

    # The first selected-vehicle generation is complete-sized but has a gap;
    # a foreign vehicle is interleaved. The second generation is coherent.
    messages = [
        value("FOREIGN", 0, 2, system=1),
        value("_HASH_CHECK", 65535, 3),
        value("A", 0, 3),
        value("STALE", 2, 3),
        value("_HASH_CHECK", 65535, 3),
        value("A", 0, 3),
        value("B", 1, 3),
    ]
    requests = []
    adapter = object.__new__(MavlinkParameterAdapter)
    adapter.timeout = 1.0
    adapter._px4_system = 8
    adapter._px4_component = 1
    adapter.connection = SimpleNamespace(
        target_system=8,
        target_component=1,
        mav=SimpleNamespace(param_request_list_send=lambda *args: requests.append(args)),
        recv_match=lambda **_kwargs: messages.pop(0) if messages else None,
    )

    result = adapter.pull_all()

    assert [item["name"] for item in result] == ["A", "B"]
    assert all(item["count"] == 2 for item in result)
    assert len(requests) == 2


def test_mavlink_shell_reads_only_fixed_release_owned_sd_files():
    from types import SimpleNamespace
    from iii_deployment.px4_parameters import MavlinkParameterAdapter

    sent = []
    messages = [
        SimpleNamespace(
            count=len(chunk), data=list(chunk) + [0] * (70 - len(chunk))
        )
        for chunk in (
            b"nsh> echo III_FILE_BEGIN; cat /fs/microsd/net.cfg; echo III_FILE_END\r\nIII_FILE_BEGIN\r\n",
            b"DEVICE=eth0\r\nIII_FILE_END\r\n",
        )
    ]
    adapter = object.__new__(MavlinkParameterAdapter)
    adapter.timeout = 1
    adapter.mavutil = SimpleNamespace(
        mavlink=SimpleNamespace(
            SERIAL_CONTROL_FLAG_EXCLUSIVE=1, SERIAL_CONTROL_FLAG_RESPOND=2
        )
    )
    adapter.connection = SimpleNamespace(
        mav=SimpleNamespace(serial_control_send=lambda *args: sent.append(args)),
        recv_match=lambda **_kwargs: messages.pop(0) if messages else None,
    )
    assert adapter.read_text_file("/fs/microsd/net.cfg") == b"DEVICE=eth0\n"
    assert sent[-1][1] == 0
    with pytest.raises(PX4ParameterError, match="not release-owned"):
        adapter.read_text_file("/fs/microsd/../../etc/passwd")


def test_mavlink_ftp_reads_fixed_release_owned_sd_file():
    from types import SimpleNamespace
    from iii_deployment.px4_parameters import MavlinkParameterAdapter

    def response(sequence, session, request_opcode, data):
        return SimpleNamespace(
            payload=(
                struct.pack("<HBBBBBBI", sequence, session, 128, len(data), request_opcode, 0, 0, 0)
                + data
                + bytes(239 - len(data))
            )
        )

    sent = []
    messages = [
        response(0, 7, 4, b""),
        response(1, 7, 5, b"DEVICE=eth0\n"),
        response(2, 7, 1, b""),
    ]
    adapter = object.__new__(MavlinkParameterAdapter)
    adapter.timeout = 1
    adapter._ftp_sequence = 0
    adapter._px4_system = 1
    adapter._px4_component = 1
    adapter.connection = SimpleNamespace(
        target_system=1,
        target_component=1,
        mav=SimpleNamespace(file_transfer_protocol_send=lambda *args: sent.append(args)),
        recv_match=lambda **_kwargs: messages.pop(0) if messages else None,
    )
    assert adapter.read_text_file("/fs/microsd/net.cfg") == b"DEVICE=eth0\n"
    assert len(sent) == 3
    assert sent[0][3][3] == 4
    assert sent[1][3][3] == 5
    assert sent[0][1:3] == (1, 1)


def test_plan_apply_verify_uses_fresh_full_backup_and_exact_confirmation(tmp_path):
    subject, adapter = make_store(tmp_path)
    snapshot = subject.pull("sim")
    plan = subject.plan(
        "sim", snapshot["snapshot_id"], selected_keys=["MPC_XY_VEL_MAX"]
    )
    with pytest.raises(PX4ParameterError, match="every planned key"):
        subject.apply(plan["plan_id"], confirmed_keys=[])

    result = subject.apply(plan["plan_id"], confirmed_keys=["MPC_XY_VEL_MAX"])
    assert result["outcome"] == "applied"
    assert result["backup_snapshot_id"] == snapshot["snapshot_id"]
    assert adapter.values["MPC_XY_VEL_MAX"] == (12.0, "REAL32")
    assert subject.verify(plan["plan_id"])["verified"] is True


def test_stale_plan_refuses_all_writes(tmp_path):
    subject, adapter = make_store(tmp_path)
    snapshot = subject.pull("real")
    plan = subject.plan(
        "real", snapshot["snapshot_id"], selected_keys=["MPC_XY_VEL_MAX"]
    )
    adapter.values["NAV_RCL_ACT"] = (2, "INT32")
    with pytest.raises(PX4ParameterError, match="changed after planning"):
        subject.apply(plan["plan_id"], confirmed_keys=["MPC_XY_VEL_MAX"])
    assert adapter.write_calls == []


@pytest.mark.parametrize("failure_mode", ["interrupt", "readback"])
def test_interrupted_or_failed_readback_restores_complete_backup(
    tmp_path, failure_mode
):
    subject, adapter = make_store(tmp_path)
    adapter.values["COM_RC_IN_MODE"] = (4, "INT32")
    snapshot = subject.pull("real")
    plan = subject.plan(
        "real",
        snapshot["snapshot_id"],
        selected_keys=["COM_RC_IN_MODE", "MPC_XY_VEL_MAX"],
    )
    if failure_mode == "interrupt":
        adapter.fail_once = "MPC_XY_VEL_MAX"
    else:
        adapter.mismatch_once = "MPC_XY_VEL_MAX"
    with pytest.raises(PX4ApplyError) as failure:
        subject.apply(
            plan["plan_id"],
            confirmed_keys=["COM_RC_IN_MODE", "MPC_XY_VEL_MAX"],
        )
    assert failure.value.result["outcome"] == "recovered"
    assert adapter.values["COM_RC_IN_MODE"] == (4, "INT32")
    assert adapter.values["MPC_XY_VEL_MAX"] == (10.0, "REAL32")


def test_named_capture_export_import_compare_and_selective_promotion(tmp_path):
    subject, adapter = make_store(tmp_path / "source")
    snapshot = subject.pull("sim")
    capture = subject.capture(
        snapshot["snapshot_id"],
        short_name="wind-tuned",
        description="Disarmed wind-test parameter set",
    )
    archive = tmp_path / "wind-tuned.iii-px4.json"
    exported = subject.export_capture(capture["capture_id"], archive)
    assert exported["capture"]["short_name"] == "wind-tuned"

    imported_store, _ = make_store(tmp_path / "destination")
    imported = imported_store.import_capture(archive)
    assert imported["capture_id"] == capture["capture_id"]
    promoted = subject.promoted_manifest(
        capture["capture_id"], accepted_keys=["MPC_XY_VEL_MAX"]
    )
    value = next(
        item["value"]
        for item in promoted["parameters"]
        if item["name"] == "MPC_XY_VEL_MAX"
    )
    assert value == 10.0
    with pytest.raises(PX4ParameterError, match="calibration/identity"):
        subject.promoted_manifest(capture["capture_id"], accepted_keys=["CAL_ACC0_ID"])

    value = json.loads(archive.read_text())
    value["snapshot"]["parameters"][0]["value"] = 999
    archive.write_bytes(canonical_json(value) + b"\n")
    with pytest.raises(PX4ParameterError, match="identity mismatch"):
        imported_store.import_capture(archive)


def test_full_promotion_adds_unexpected_inventory_as_never_write_preserved(tmp_path):
    subject, adapter = make_store(tmp_path)
    adapter.values["COM_MODE0_HASH"] = (123456, "INT32")
    snapshot = subject.pull("sim")
    capture = subject.capture(
        snapshot["snapshot_id"],
        short_name="steady-runtime",
        description="Complete inventory after dynamic modules started",
    )

    promoted = subject.promoted_manifest(
        capture["capture_id"],
        accepted_keys=["COM_MODE0_HASH", "MPC_XY_VEL_MAX"],
        include_unexpected_as_preserved=True,
    )

    by_name = {item["name"]: item for item in promoted["parameters"]}
    assert by_name["COM_MODE0_HASH"] == {
        "name": "COM_MODE0_HASH",
        "mav_type": "INT32",
        "value": None,
        "classification": "calibration-identity",
        "enforcement": "preserve",
        "tolerance": 0,
    }
    assert promoted["inventory"]["parameter_count"] == 5
    assert promoted["inventory"]["source_sha256"] == snapshot["snapshot_id"]


def test_monitor_debounces_events_reconciles_periodically_and_never_pulls_armed(
    tmp_path,
):
    subject, adapter = make_store(tmp_path)
    monitor = PX4ParameterMonitor(subject, profile="real")
    first = monitor.tick(0.0)
    assert first is not None and first["changed"] is True
    baseline_pulls = adapter.pull_calls
    monitor.event(10.0)
    assert monitor.tick(11.999) is None
    assert adapter.pull_calls == baseline_pulls
    second = monitor.tick(12.0)
    assert second is not None and second["changed"] is False
    assert monitor.tick(71.999) is None
    assert monitor.tick(72.0) is not None

    adapter.armed = True
    pulls = adapter.pull_calls
    monitor.event(80.0)
    assert monitor.tick(82.0) is None
    assert monitor.clean_end() is None
    assert adapter.pull_calls == pulls
    adapter.armed = False
    assert monitor.clean_end() is not None
