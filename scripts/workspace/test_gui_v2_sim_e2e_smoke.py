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


def test_browser_password_falls_back_to_runtime_token_file(monkeypatch, tmp_path):
    token_file = tmp_path / "runtime-token"
    token_file.write_text("provisioned-secret\n", encoding="ascii")
    monkeypatch.delenv("III_RUNTIME_API_BROWSER_PASSWORD", raising=False)
    monkeypatch.setenv("III_RUNTIME_API_TOKEN_FILE", str(token_file))

    assert smoke.default_browser_password() == "provisioned-secret"


def test_explicit_browser_password_takes_precedence(monkeypatch, tmp_path):
    token_file = tmp_path / "runtime-token"
    token_file.write_text("runtime-token\n", encoding="ascii")
    monkeypatch.setenv("III_RUNTIME_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("III_RUNTIME_API_BROWSER_PASSWORD", "operator-secret")

    assert smoke.default_browser_password() == "operator-secret"


def test_configuration_revision_requires_integer_revision():
    assert smoke.configuration_revision(
        {"latest": {"manifest": {"status": {"tuning_revision": 17}}}}
    ) == 17
    with pytest.raises(smoke.SmokeFailure, match="tuning revision"):
        smoke.configuration_revision(
            {"latest": {"manifest": {"status": {"tuning_revision": True}}}}
        )


@pytest.mark.parametrize("active_flag", ["run_mutating_workflows", "run_flight_commands", "run_inspection_cycle"])
def test_any_state_changing_workflow_requires_failure_recovery(active_flag):
    args = SimpleNamespace(
        run_mutating_workflows=False,
        run_flight_commands=False,
        run_inspection_cycle=False,
    )
    setattr(args, active_flag, True)

    assert smoke.recovery_required(args)


def test_read_only_workflow_does_not_request_vehicle_recovery():
    args = SimpleNamespace(
        run_mutating_workflows=False,
        run_flight_commands=False,
        run_inspection_cycle=False,
    )

    assert not smoke.recovery_required(args)


def test_hil_profile_is_an_explicit_smoke_target():
    args = smoke.parse_args(["--expected-profile", "hil"])

    assert args.expected_profile == "hil"


def test_cable_aware_fixture_preserves_recorded_clearance_altitude():
    assert smoke.fixture_flight_altitude(0.611, cable_aware=True) == 0.611
    assert smoke.fixture_flight_altitude(0.611, cable_aware=False) == pytest.approx(0.671)


def test_default_http_timeout_exceeds_proxy_operation_budget(monkeypatch):
    monkeypatch.delenv("III_GUI_V2_E2E_HTTP_TIMEOUT_SEC", raising=False)

    args = smoke.parse_args([])

    assert args.http_timeout_s > 180.0


def test_flight_workflow_waits_for_stable_arming_readiness_before_dispatch():
    runner = object.__new__(smoke.SmokeRunner)
    calls = []

    def wait_for_stable(name, path, headers, predicate, **kwargs):
        calls.append(("wait", name, path, kwargs))
        ready = {
            "arming_checks_passed": True,
            "latest": {"command_transport": {"command_available": True}},
            "armed": True,
            "in_air": True,
            "nav_state": "hold",
            "telemetry_fields": {
                "armed": {
                    "value": True,
                    "freshness": "fresh",
                    "source_availability": "available",
                    "disagreement": False,
                },
                "in_air": {
                    "value": True,
                    "freshness": "fresh",
                    "source_availability": "available",
                    "disagreement": False,
                },
                "nav_state": {
                    "value": "hold",
                    "freshness": "fresh",
                    "source_availability": "available",
                    "disagreement": False,
                },
            },
        }
        if name == "px4-landed-disarmed":
            ready["armed"] = False
            ready["in_air"] = False
            ready["telemetry_fields"]["armed"]["value"] = False
            ready["telemetry_fields"]["in_air"]["value"] = False
        assert predicate(ready)
        return ready

    runner.wait_for_stable_state = wait_for_stable
    runner.wait_for_state = lambda *args, **kwargs: calls.append(("wait_state", args[0]))
    runner.dispatch_command = lambda name, command_id, parameters, headers: calls.append(
        ("dispatch", name, command_id)
    )

    runner.run_flight_commands({})

    assert calls[0][0:3] == ("wait", "px4-arming-ready", "/proxy/vehicle/status")
    assert calls[0][3]["consecutive_samples"] == 3
    assert calls[1] == ("dispatch", "px4-arm", "px4.arm")


def test_frontend_url_uses_provisioned_gc_port(monkeypatch):
    monkeypatch.delenv("III_GC_FRONTEND_URL", raising=False)
    monkeypatch.setenv("III_GC_FRONTEND_PORT", "5173")

    args = smoke.parse_args([])

    assert args.frontend_url == "http://127.0.0.1:5173"


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


def test_configuration_retry_refreshes_revision_after_compare_and_swap_race():
    runner = object.__new__(smoke.SmokeRunner)
    states = iter(
        [
            {
                "latest": {
                    "manifest": {
                        "status": {"tuning_revision": 1},
                        "nodes": [
                            {"groups": [{"parameters": [{"name": "/safe", "current_value": 1.0}]}]}
                        ],
                    },
                }
            },
            {
                "latest": {
                    "manifest": {
                        "status": {"tuning_revision": 2},
                        "nodes": [
                            {"groups": [{"parameters": [{"name": "/safe", "current_value": 1.0}]}]}
                        ],
                    },
                }
            },
        ]
    )
    runner.get_state = lambda *_args: next(states)
    calls = []

    def dispatch(_name, _command, parameters, *_args, **_kwargs):
        calls.append(parameters)
        if len(calls) == 1:
            return {
                "accepted": False,
                "rejection": {
                    "code": "forbidden",
                    "message": "stale expected revision",
                    "retryable": False,
                },
            }
        return {"accepted": True}

    runner.dispatch_command = dispatch
    result = runner.apply_configuration_edit_with_retry(
        "configuration-apply",
        {"node_id": "node", "name": "/safe", "value": 1.1},
        {},
        timeout_s=1,
        interval_s=0,
    )

    assert result["accepted"] is True
    assert [call["expected_revision"] for call in calls] == [1, 2]


def test_mutating_smoke_accepts_expected_missing_sensor_degradation():
    runner = object.__new__(smoke.SmokeRunner)
    runner.args = SimpleNamespace(expected_profile="hil", rosbag_observation_s=0.0)
    calls = []

    def dispatch(name, command_id, parameters, headers, require_accepted=True):
        calls.append((command_id, parameters, require_accepted))
        if command_id == "powerline.overview.update":
            return {
                "accepted": False,
                "rejection": {
                    "code": "degraded_state",
                    "message": "at least 4 live powerline lines are required; mapper has no overview",
                },
            }
        if command_id == "rosbag.start":
            return {
                "accepted": True,
                "result": {"rosbag": {"recording_id": "smoke-recording"}},
            }
        return {"accepted": True}

    runner.dispatch_command = dispatch
    runner.wait_for_state = lambda *_args, **_kwargs: {
        "recording": True,
        "recording_id": "smoke-recording",
        "size_bytes": 1024,
    }
    runner.get_state = lambda *_args, **_kwargs: {
        "latest": {
            "recordings": [
                {"recording_id": "smoke-recording", "size_bytes": 1024}
            ]
        }
    }
    runner.round_trip_safe_configuration_parameter = lambda _headers: None
    runner.run_mutating_workflows({})

    assert calls[0] == ("runtime.boot", {"profile": "hil"}, True)
    assert any(command_id == "powerline.overview.update" and not required for command_id, _, required in calls)
    assert any(command_id == "custom_operation.hover.start" and not required for command_id, _, required in calls)
    assert all(
        require_accepted
        for command_id, _, require_accepted in calls
        if command_id not in smoke.EXPECTED_BENCH_DEGRADED_COMMANDS
    )


def test_mutating_smoke_proves_manual_recording_then_runs_guarded_overview_capture():
    command_ids = [command_id for _, command_id, _ in smoke.MUTATING_WORKFLOW_COMMANDS]

    start_recording = command_ids.index("rosbag.start")
    update_overview = command_ids.index("powerline.overview.update")
    stop_recording = command_ids.index("rosbag.stop")
    stop_mapper = command_ids.index("perception.pl_mapper.stop")

    assert start_recording < stop_recording < update_overview < stop_mapper
    start_parameters = smoke.MUTATING_WORKFLOW_COMMANDS[start_recording][2]
    assert start_parameters == {
        "all_topics": False,
        "topics": [
            "/clock",
            "/fmu/out/vehicle_status_v1",
            "/perception/pl_mapper/powerline",
            "/perception/pl_mapper/state",
            "/supervision/system_health",
        ],
    }


def test_custom_operation_smoke_start_carries_hold_confirmation():
    _, _, parameters = next(
        command
        for command in smoke.MUTATING_WORKFLOW_COMMANDS
        if command[1] == "custom_operation.hover.start"
    )

    assert parameters["hold_confirmed"] is True


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


def test_flight_smoke_waits_for_fused_state_between_commands():
    runner = object.__new__(smoke.SmokeRunner)
    events = []

    runner.dispatch_command = lambda name, command_id, parameters, headers: events.append(
        ("command", name, command_id)
    )

    def wait_stable(name, path, headers, predicate, **kwargs):
        states = {
            "px4-arming-ready": {
                "arming_checks_passed": True,
                "latest": {"command_transport": {"command_available": True}},
            },
            "px4-armed": {
                "armed": True,
                "telemetry_fields": {
                    "armed": {
                        "value": True,
                        "freshness": "fresh",
                        "source_availability": "available",
                        "disagreement": False,
                    }
                },
            },
            "px4-landed-disarmed": {
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
            },
            "px4-holding": {
                "nav_state": "hold",
                "in_air": True,
                "telemetry_fields": {
                    "nav_state": {
                        "value": "hold",
                        "freshness": "fresh",
                        "source_availability": "available",
                        "disagreement": False,
                    }
                },
            },
        }
        state = states[name]
        assert predicate(state)
        events.append(("stable", name))
        return state

    def wait(name, path, headers, predicate, **kwargs):
        states = {
            "px4-airborne": {
                "armed": True,
                "in_air": True,
                "telemetry_fields": {
                    "in_air": {
                        "value": True,
                        "freshness": "fresh",
                        "source_availability": "available",
                        "disagreement": False,
                    }
                },
            },
        }
        state = states[name]
        assert predicate(state)
        events.append(("state", name))
        return state

    runner.wait_for_stable_state = wait_stable
    runner.wait_for_state = wait

    runner.run_flight_commands({})

    assert events == [
        ("stable", "px4-arming-ready"),
        ("command", "px4-arm", "px4.arm"),
        ("stable", "px4-armed"),
        ("command", "px4-takeoff", "px4.takeoff"),
        ("state", "px4-airborne"),
        ("command", "px4-hold", "px4.hold"),
        ("stable", "px4-holding"),
        ("command", "px4-land", "px4.land"),
        ("stable", "px4-landed-disarmed"),
    ]


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
        expected_profile="sim",
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
    runner.args = SimpleNamespace(fixture_resolver="resolver", expected_profile="sim")
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
    runner.args = SimpleNamespace(fixture_resolver="resolver", expected_profile="sim")
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
    runner.args = SimpleNamespace(fixture_resolver="resolver", expected_profile="sim")
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


def test_visible_live_line_count_uses_provider_visibility_contract():
    state = {
        "live_geometry": {
            "lines": [
                {"id": 1, "in_field_of_view": True},
                {"id": 2, "in_field_of_view": False},
                {"id": 3, "in_field_of_view": True},
                {"id": 4},
            ]
        }
    }

    assert smoke.visible_live_line_count(state) == 2


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
        "current_value": 0.5,
        "persisted_value": 0.5,
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
    assert smoke.numeric_values_match(0.5, 0.5)
    assert not smoke.numeric_values_match(True, 1.0)
    assert not smoke.configuration_manifest_available({"source_availability": "unavailable"})


def test_inspection_battery_reset_uses_workspace_container_and_records_evidence(tmp_path, monkeypatch):
    runner = object.__new__(smoke.SmokeRunner)
    runner.workspace = tmp_path
    runner.artifacts = tmp_path
    runner.args = SimpleNamespace(expected_profile="hil")
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
    assert "--px4-system-address udpin://0.0.0.0:14551" in calls[1][0][-1]
    assert "III_MAVSDK_SERVER_PORT=50052" in calls[1][0][-1]
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


def test_hil_battery_reset_accepts_only_exact_px4_token_acknowledgement():
    accepted = {
        "success": False,
        "message": "battery reset was acknowledged but no battery status was observed",
        "data": {
            "reset_token": 7,
            "acknowledgement_token": 7,
            "target_remaining_pct": 65.0,
            "initial_percentage_parameter": {"param_value": 65.0},
            "battery_after": None,
            "observed_remaining_pct": None,
        },
    }

    assert smoke.acknowledged_hil_battery_reset(accepted)
    assert not smoke.acknowledged_hil_battery_reset(
        {**accepted, "data": {**accepted["data"], "acknowledgement_token": 6}}
    )
    assert not smoke.acknowledged_hil_battery_reset(
        {**accepted, "message": "parameter transport failed"}
    )


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
