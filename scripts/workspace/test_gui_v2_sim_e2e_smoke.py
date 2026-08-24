from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
import json
import threading

import pytest


SCRIPT = Path(__file__).with_name("gui_v2_sim_e2e_smoke.py")
SPEC = spec_from_file_location("gui_v2_sim_e2e_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_active_mission_mode_uses_typed_mode_registry():
    state = {
        "mission_active": True,
        "owned_mode": "wrong-fallback",
        "modes": [
            {"mode_key": "inspection_demo", "active": False},
            {"mode_key": "reach_cable", "active": True},
        ],
    }
    assert smoke.active_mission_mode(state) == "reach_cable"


def test_operation_completion_rejects_failed_terminal_event():
    state = {
        "active_operation_id": None,
        "latest": {"operation_active": False, "operation_events": [{"status": "failed", "message": "planner failed"}]},
    }
    with pytest.raises(smoke.SmokeFailure, match="planner failed"):
        smoke.operation_finished(state)


def test_operation_completion_understands_typed_result_events():
    succeeded = {
        "active_operation_id": None,
        "latest": {
            "operation_active": False,
            "operation_events": [{"event_type": "result", "payload": {"result": {"success": True, "status": 4}}}],
        },
    }
    rejected = {
        "active_operation_id": None,
        "latest": {
            "operation_active": False,
            "operation_events": [
                {
                    "event_type": "result",
                    "payload": {"result": {"success": False, "status": "rejected", "error": "mode inactive"}},
                }
            ],
        },
    }

    assert smoke.operation_finished(succeeded)
    with pytest.raises(smoke.SmokeFailure, match="mode inactive"):
        smoke.operation_finished(rejected)


def test_battery_assertions_require_directional_change():
    smoke.assert_battery_depleted({"battery_remaining": 0.5}, {"battery_remaining": 0.49})
    smoke.assert_battery_charged({"battery_remaining": 0.49}, {"battery_remaining": 0.51})
    with pytest.raises(smoke.SmokeFailure, match="deplete"):
        smoke.assert_battery_depleted({"battery_remaining": 0.5}, {"battery_remaining": 0.5})
    with pytest.raises(smoke.SmokeFailure, match="charge"):
        smoke.assert_battery_charged({"battery_remaining": 0.5}, {"battery_remaining": 0.5})


def test_battery_increased_requires_a_telemetry_significant_delta():
    before = {"battery_remaining": 0.5}

    assert smoke.battery_increased(before, {"battery_remaining": 0.503})
    assert not smoke.battery_increased(before, {"battery_remaining": 0.501})


def test_overview_completion_requires_powerline_and_exact_two_pylons():
    valid = {"valid": True, "pylon_overview": {"valid": True, "pylon_count": 2}}
    assert smoke.overviews_complete(valid)
    assert not smoke.overviews_complete({**valid, "valid": False})
    assert not smoke.overviews_complete({"valid": True, "pylon_overview": {"valid": True, "pylon_count": 1}})


@pytest.mark.parametrize(
    "state",
    [
        {"status": "custom_operation_active"},
        {"latest": {"control_owner": "custom_operation"}},
    ],
)
def test_operation_mode_active_recognizes_existing_control(state):
    assert smoke.operation_mode_active(state)


def test_operation_mode_active_rejects_unowned_state():
    assert not smoke.operation_mode_active({"status": "inactive", "latest": {"control_owner": None}})
    assert not smoke.operation_mode_active({"status": "custom_operation_idle", "latest": {"control_owner": "unknown"}})


def test_request_ids_do_not_replay_between_runs():
    first = smoke.build_request_id("run-a", 27, "inspection-arm")
    second = smoke.build_request_id("run-b", 27, "inspection-arm")

    assert first == "smoke-run-a-27-inspection-arm"
    assert first != second


def test_frontend_session_payload_matches_browser_storage_contract():
    payload = smoke.frontend_session_payload(
        token="token",
        endpoint_id="endpoint",
        runtime_name="Sim Runtime",
        runtime_id="runtime-sim",
        system_id="aircraft-sim",
        profile="sim",
    )

    assert payload == {
        "token": "token",
        "endpointId": "endpoint",
        "runtimeName": "Sim Runtime",
        "runtimeId": "runtime-sim",
        "systemId": "aircraft-sim",
        "profile": "sim",
    }


def test_retryable_command_retries_with_unique_attempt_names_until_accepted():
    runner = object.__new__(smoke.SmokeRunner)
    calls = []
    results = iter(
        [
            {"accepted": False, "rejection": {"retryable": True, "message": "settling"}},
            {"accepted": True},
        ]
    )

    def dispatch(name, *_args, **_kwargs):
        calls.append(name)
        return next(results)

    runner.dispatch_command = dispatch
    result = runner.dispatch_retryable_command(
        "pylon-capture", "pylon.capture_current", {"pylon_id": 1}, {}, timeout_s=1, interval_s=0
    )

    assert result["accepted"] is True
    assert calls == ["pylon-capture-attempt-1", "pylon-capture-attempt-2"]


def test_retryable_command_fails_immediately_on_non_retryable_rejection():
    runner = object.__new__(smoke.SmokeRunner)
    runner.dispatch_command = lambda *_args, **_kwargs: {
        "accepted": False,
        "rejection": {"retryable": False, "message": "invalid pylon id"},
    }

    with pytest.raises(smoke.SmokeFailure, match="invalid pylon id"):
        runner.dispatch_retryable_command(
            "pylon-capture", "pylon.capture_current", {"pylon_id": 3}, {}, timeout_s=1, interval_s=0
        )


def test_concurrent_heartbeats_are_serialized():
    runner = object.__new__(smoke.SmokeRunner)
    runner.args = SimpleNamespace(proxy_url="http://proxy")
    runner._last_heartbeat_at = 0.0
    runner._heartbeat_lock = threading.Lock()
    calls = []
    runner.http_json = lambda *args, **kwargs: calls.append((args, kwargs))

    threads = [threading.Thread(target=runner.heartbeat, args=({},)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1


def test_telemetry_field_match_requires_fresh_agreeing_evidence():
    valid = {
        "telemetry_fields": {
            "armed": {
                "value": True,
                "freshness": "fresh",
                "source_availability": "available",
                "disagreement": False,
            }
        }
    }

    assert smoke.telemetry_field_matches(valid, "armed", True)
    assert not smoke.telemetry_field_matches(valid, "armed", False)
    assert not smoke.telemetry_field_matches(
        {
            "telemetry_fields": {
                "armed": {
                    **valid["telemetry_fields"]["armed"],
                    "disagreement": True,
                }
            }
        },
        "armed",
        True,
    )


def test_landed_safe_state_requires_fresh_disarmed_and_not_in_air_evidence():
    state = {
        "armed": False,
        "in_air": False,
        "telemetry_fields": {
            "armed": {
                "value": False,
                "freshness": "fresh",
                "source_availability": "available",
                "disagreement": False,
            },
            "in_air": {
                "value": False,
                "freshness": "fresh",
                "source_availability": "available",
                "disagreement": False,
            },
        },
    }

    assert smoke.vehicle_landed_and_disarmed(state)
    assert not smoke.vehicle_landed_and_disarmed({**state, "armed": True})


def test_operation_completion_can_be_scoped_to_exact_action():
    state = {
        "active_operation_id": None,
        "latest": {
            "operation_active": False,
            "operation_events": [
                {
                    "event_type": "result",
                    "operation_id": "old-action",
                    "payload": {"result": {"success": True}},
                },
                {
                    "event_type": "result",
                    "operation_id": "new-action",
                    "payload": {"result": {"success": True}},
                },
            ],
        },
    }

    assert smoke.operation_started_or_finished(state, "new-action")
    assert smoke.operation_finished(state, operation_id="new-action")
    assert not smoke.operation_finished(state, operation_id="missing-action")


def test_fixture_resolution_rejects_stored_ros_fallback(tmp_path, monkeypatch):
    runner = object.__new__(smoke.SmokeRunner)
    runner.workspace = tmp_path
    runner.artifacts = tmp_path
    runner.args = SimpleNamespace(
        fixture_resolver="resolver",
    )
    result = SimpleNamespace(
        returncode=0,
        stdout='{"success":true,"data":{"target_source":"stored_ros_world_pose"}}',
        stderr="",
    )
    monkeypatch.setattr(smoke.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(smoke.SmokeFailure, match="non-authoritative source"):
        runner.resolve_fixture("fixture")


def test_fixture_resolution_preserves_authoritative_mapped_pose(tmp_path, monkeypatch):
    runner = object.__new__(smoke.SmokeRunner)
    runner.workspace = tmp_path
    runner.artifacts = tmp_path
    runner.args = SimpleNamespace(fixture_resolver="resolver")
    target = {
        "target_source": "gazebo_ground_truth_mapped_to_live_ros_world",
        "frame_id": "world",
        "x": 1.0,
        "y": 2.0,
        "z": 0.67,
        "yaw": 0.25,
        "live_mapping": {"offset": {"z": 0.06}},
    }
    result = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True, "data": target}),
        stderr="",
    )
    monkeypatch.setattr(smoke.subprocess, "run", lambda *_args, **_kwargs: result)

    assert runner.resolve_fixture("fixture") == target


def test_fixture_application_accepts_authoritative_setup_evidence(tmp_path, monkeypatch):
    runner = object.__new__(smoke.SmokeRunner)
    runner.workspace = tmp_path
    runner.artifacts = tmp_path
    runner.args = SimpleNamespace(fixture_resolver="resolver")
    target = {
        "setup_only": True,
        "fixture_id": "pos_pylon_1",
        "gazebo_world": "hca_full_pylon_setup",
        "gazebo_model": "d4s_dc_drone_0",
        "gazebo_pose": {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.25},
    }
    result = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True, "data": target}),
        stderr="",
    )
    monkeypatch.setattr(smoke.subprocess, "run", lambda *_args, **_kwargs: result)

    assert runner.resolve_fixture("pos_pylon_1", apply=True) == target


def test_fixture_application_rejects_missing_setup_evidence(tmp_path, monkeypatch):
    runner = object.__new__(smoke.SmokeRunner)
    runner.workspace = tmp_path
    runner.artifacts = tmp_path
    runner.args = SimpleNamespace(fixture_resolver="resolver")
    result = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True, "data": {"setup_only": True}}),
        stderr="",
    )
    monkeypatch.setattr(smoke.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(smoke.SmokeFailure, match="authoritative Gazebo setup evidence"):
        runner.resolve_fixture("pos_pylon_1", apply=True)


def test_acceptance_rosbag_topics_exclude_bulk_sensor_streams():
    assert "/mission/status" in smoke.INSPECTION_EVIDENCE_TOPICS
    assert "/perception/pl_mapper/powerline" in smoke.INSPECTION_EVIDENCE_TOPICS
    assert all("image" not in topic for topic in smoke.INSPECTION_EVIDENCE_TOPICS)
    assert all("points" not in topic for topic in smoke.INSPECTION_EVIDENCE_TOPICS)


def test_seed_sim_pylon_overview_uses_mapped_fixtures_without_applying_them():
    runner = object.__new__(smoke.SmokeRunner)
    resolutions = []
    calls = []
    artifacts = []
    runner.resolve_fixture = lambda fixture_id, **kwargs: resolutions.append((fixture_id, kwargs)) or {
        "x": 10.0 + len(resolutions),
        "y": 20.0 + len(resolutions),
        "target_source": "gazebo_ground_truth_mapped_to_live_ros_world",
    }
    runner.call_sim_mcp = lambda tool, arguments, **kwargs: calls.append((tool, arguments, kwargs)) or {"success": True}
    runner.record_structured_artifact = lambda name, value: artifacts.append((name, value))

    runner.seed_sim_pylon_overview(("pos_pylon_1", "pos_pylon_2"))

    assert resolutions == [("pos_pylon_1", {}), ("pos_pylon_2", {})]
    assert [tool for tool, _, _ in calls] == [
        "perception.clear_pylon_overview",
        "perception.store_pylon_overview",
        "perception.store_pylon_overview",
    ]
    assert calls[1][1]["pylon_id"] == 1
    assert calls[2][1]["pylon_id"] == 2
    assert all(value["setup_only"] for _, value in artifacts)


def test_configuration_helpers_require_authoritative_manifest_and_find_parameter():
    parameter = {
        "node_id": "trajectory_generator",
        "name": "/control/trajectory_interpolator/interpolation_avg_velocity_m_s",
        "current_value": 0.75,
        "persisted_value": 0.75,
    }
    state = {
        "source_availability": "available",
        "latest": {
            "manifest": {
                "status": {"configuration_server_available": True},
                "nodes": [{"groups": [{"parameters": [parameter]}]}],
            }
        },
    }

    assert smoke.configuration_manifest_available(state)
    assert smoke.configuration_parameter(state, parameter["name"]) == parameter
    assert smoke.numeric_values_match(0.75, 0.75)
    assert not smoke.numeric_values_match(True, 1.0)
    assert not smoke.configuration_manifest_available({"source_availability": "unavailable"})


def test_inspection_battery_reset_uses_workspace_container_and_records_evidence(tmp_path, monkeypatch):
    runner = object.__new__(smoke.SmokeRunner)
    runner.workspace = tmp_path
    runner.artifacts = tmp_path
    runner.step_index = 0
    runner.summary = {"steps": []}
    calls = []
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="container-id\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "message": "reset", "data": {"observed_remaining_pct": 100.0}}),
                stderr="",
            ),
        ]
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return next(results)

    monkeypatch.setattr(smoke.subprocess, "run", run)

    runner.reset_sim_battery(100.0)

    assert calls[0][0][:3] == ["docker", "ps", "--filter"]
    assert calls[1][0][:5] == ["docker", "exec", "--user", "iii", "container-id"]
    assert "battery.reset" in calls[1][0][-1]
    assert json.loads((tmp_path / "01-inspection-battery-reset.json").read_text())["success"] is True


def test_simulated_battery_reset_visibility_allows_accelerated_discharge():
    state = {
        "battery_remaining": 0.905,
        "telemetry_fields": {
            "battery_remaining": {"freshness": "fresh", "source_availability": "available"}
        },
    }

    assert smoke.simulated_battery_reset_visible(state, 100.0)
    state["battery_remaining"] = 0.84
    assert not smoke.simulated_battery_reset_visible(state, 100.0)
    state["battery_remaining"] = 0.95
    state["telemetry_fields"]["battery_remaining"]["freshness"] = "stale"
    assert not smoke.simulated_battery_reset_visible(state, 100.0)


def test_mission_modes_selectable_requires_every_registered_px4_bit():
    mission = {
        "modes": [
            {"mode_id": 23, "registered": True},
            {"mode_id": 24, "registered": True},
            {"mode_id": 25, "registered": True},
            {"mode_id": 26, "registered": True},
        ]
    }
    vehicle = {
        "latest": {
            "ros_uxrce": {
                "raw": {
                    "vehicle_status": {
                        "can_set_nav_states_mask": sum(1 << mode_id for mode_id in range(23, 27))
                    }
                }
            }
        }
    }

    assert smoke.mission_modes_selectable(vehicle, mission)
    vehicle["latest"]["ros_uxrce"]["raw"]["vehicle_status"]["can_set_nav_states_mask"] &= ~(1 << 24)
    assert not smoke.mission_modes_selectable(vehicle, mission)
    assert not smoke.mission_modes_selectable({}, mission)
    assert not smoke.mission_modes_selectable(vehicle, {"modes": []})


def test_select_chromium_page_matches_frontend_origin():
    pages = [
        {"type": "page", "url": "chrome://newtab/", "id": "newtab"},
        {"type": "page", "url": "http://127.0.0.1:5174/mission", "id": "frontend"},
    ]

    assert smoke.select_chromium_page(pages, "http://127.0.0.1:5174")["id"] == "frontend"
    assert smoke.select_chromium_page(pages, "http://127.0.0.1:9999") is None
