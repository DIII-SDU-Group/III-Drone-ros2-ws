"""Failure propagation between actual HIL driver mode waits."""

import importlib.util
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("hil_cycle_driver", Path(__file__).with_name("hil_inspection_cycle_driver.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_failed_reach_cable_ends_charging_wait_for_cleanup(monkeypatch):
    driver = SimpleNamespace(
        mode_status={"reach_cable": {"active": False, "tree_finished": True, "tree_success": False}},
        _mode_status_floor={},
    )
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="reach_cable failed before cable_charging"):
        module.Driver.wait_mode(driver, "cable_charging", lambda value: value.get("active"), failed_predecessor="reach_cable")


def test_successful_reach_cable_allows_charging_activation(monkeypatch):
    driver = SimpleNamespace(
        mode_status={
            "reach_cable": {"active": False, "tree_finished": True, "tree_success": True},
            "cable_charging": {"active": True, "tree_running": True, "_stamp": (100, 0)},
        },
        _mode_status_floor={},
    )
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    result = module.Driver.wait_mode(driver, "cable_charging", lambda value: value.get("active"), failed_predecessor="reach_cable")
    assert result["active"] is True


@pytest.mark.parametrize("fresh,power,latched,expected", [
    (True, 120.0, True, True),
    (False, 120.0, True, False),
    (True, 0.0, True, False),
    (True, 120.0, False, False),
])
def test_charging_requires_fresh_positive_power_and_latch(monkeypatch, fresh, power, latched, expected):
    import itertools
    ticks = itertools.count(0.0, 0.1)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(charging_samples={})

    def spin(*args, **kwargs):
        stamp = module.time.monotonic() if fresh else -10.0
        driver.charging_samples = {
            "charger_status": (stamp, module.ChargerStatus.CHARGER_STATUS_CHARGING),
            "charging_power": (stamp, power),
            "sim_state": (stamp, "latched=1;conductor=cable" if latched else "latched=0"),
        }

    monkeypatch.setattr(module, "spin_ready", spin)
    if expected:
        assert module.Driver.wait_charging_evidence(driver, timeout_sec=1.0)["charging_power_w"] == power
    else:
        with pytest.raises(TimeoutError, match="positive charging-power evidence"):
            module.Driver.wait_charging_evidence(driver, timeout_sec=1.0)


def test_native_arm_rejection_is_recorded_and_propagated():
    response = {"accepted": False, "message": "PX4 arming checks have not passed"}
    driver = SimpleNamespace(
        runtime_client=SimpleNamespace(command=lambda *args: response),
        native_command_receipts=[],
    )
    with pytest.raises(RuntimeError, match="Native px4.arm rejected.*arming checks"):
        module.Driver.native_flight_command(driver, "px4.arm")
    assert driver.native_command_receipts == [{"command_id": "px4.arm", "response": response}]


def test_runtime_api_wait_services_ros_callbacks_and_propagates_result(monkeypatch):
    completed = threading.Event()
    callback_count = 0
    charging_receipt = [time.monotonic()]

    def delayed_request():
        time.sleep(2.15)
        completed.set()
        return {"accepted": True, "request_id": "receipt-1"}

    def spin_once(*args, **kwargs):
        nonlocal callback_count
        callback_count += 1
        charging_receipt[0] = time.monotonic()
        time.sleep(0.01)

    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", spin_once)
    driver = SimpleNamespace()
    result = module.Driver._runtime_api_call(driver, delayed_request, timeout_sec=3.0)
    assert result == {"accepted": True, "request_id": "receipt-1"}
    assert callback_count >= 3
    assert completed.is_set()
    assert time.monotonic() - charging_receipt[0] <= 0.2


def test_runtime_api_wait_propagates_worker_exception(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: time.sleep(0.01))
    driver = SimpleNamespace()

    def fail_request():
        raise RuntimeError("Runtime API command rejected")

    with pytest.raises(RuntimeError, match="Runtime API command rejected"):
        module.Driver._runtime_api_call(driver, fail_request, timeout_sec=1.0)


def test_ingress_disarm_fails_without_rearming():
    driver = SimpleNamespace(wait_vehicle=lambda **kwargs: SimpleNamespace(
        failsafe=False, arming_state=module.VehicleStatus.ARMING_STATE_DISARMED,
    ))
    with pytest.raises(RuntimeError, match="disarmed during ingress"):
        module.Driver.ensure_armed_handoff(driver)


@pytest.mark.parametrize("profile,system_id,endpoint", [
    ("hil", 8, "udpin://0.0.0.0:14544"),
    ("sim", 1, "udpin://0.0.0.0:14540"),
])
def test_native_prelude_uses_arm_then_takeoff_with_ros_postconditions(monkeypatch, profile, system_id, endpoint):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    status = SimpleNamespace(system_id=system_id, failsafe=False,
        arming_state=module.VehicleStatus.ARMING_STATE_DISARMED,
        nav_state=module.VehicleStatus.NAVIGATION_STATE_AUTO_LOITER)
    commands = []
    api_arm_samples = 0
    api_arm_ready = False
    takeoff_requested = False
    api_hold_samples = 0
    def api_state():
        nonlocal api_arm_samples, api_arm_ready, api_hold_samples
        if status.arming_state == module.VehicleStatus.ARMING_STATE_ARMED:
            api_arm_samples += 1
            api_arm_ready = api_arm_samples >= 3
        if takeoff_requested:
            api_hold_samples += 1
        nav_state = "hold" if api_hold_samples >= 3 else "takeoff"
        values = {"armed": api_arm_ready, "in_air": takeoff_requested, "nav_state": nav_state}
        return {
            **values, "freshness": "fresh",
            "telemetry_fields": {name: {
                "value": value, "freshness": "fresh",
                "source_availability": "available", "disagreement": False,
            } for name, value in values.items()},
            "latest": {"dangerous_commands_allowed": api_arm_ready,
                       "command_transport": {"endpoint": endpoint}},
        }
    def command(command_id, parameters=None):
        nonlocal takeoff_requested
        commands.append((command_id, parameters))
        if command_id == "px4.arm":
            status.arming_state = module.VehicleStatus.ARMING_STATE_ARMED
        if command_id == "px4.takeoff":
            assert api_arm_ready, "takeoff raced the Runtime API fused armed state"
            takeoff_requested = True
    driver = SimpleNamespace(
        runtime_client=SimpleNamespace(
            identity=lambda: {"profile": profile},
            vehicle_status=api_state),
        wait_vehicle=lambda **kwargs: status,
        native_flight_command=command,
        wait_native_vehicle_state=lambda *args, **kwargs: module.Driver.wait_native_vehicle_state(driver, *args, **kwargs),
        native_state_receipts=[],
        land_detected=SimpleNamespace(landed=False),
        local_position=object(),
    )
    monkeypatch.setenv("III_PX4_SYSTEM_ADDRESS", endpoint)
    module.Driver.prepare_airborne(driver, None)
    assert driver.px4_system_address == endpoint
    assert api_arm_samples >= 3
    assert api_hold_samples == 3, "prelude returned before the API observed Hold"
    assert commands == [("px4.arm", None), ("px4.takeoff", {"altitude_m": 2.0})]


@pytest.mark.parametrize("identity,endpoint,override,system_id", [
    ({"profile": "unknown"}, "udpin://0.0.0.0:14540", None, None),
    ({"profile": "sim"}, "udpin://0.0.0.0:14544", None, None),
    ({"profile": "sim"}, "udpin://0.0.0.0:14540", "udpin://0.0.0.0:14544", None),
    ({"profile": "sim"}, "udpin://0.0.0.0:14540", None, 8),
])
def test_px4_target_rejects_unknown_or_mismatched_profile(identity, endpoint, override, system_id):
    with pytest.raises(RuntimeError):
        module.select_px4_target(identity, endpoint, override, system_id)


def test_px4_target_accepts_explicit_endpoint_only_when_runtime_matches():
    endpoint = "udpin://192.0.2.10:14540"
    assert module.select_px4_target(
        {"profile": "sim"}, endpoint, configured_endpoint=endpoint, system_id=1
    ) == (endpoint, 1)


def test_dwell_spins_until_minimum_duration_while_mode_stays_running(monkeypatch):
    import itertools
    ticks = itertools.count(0.0, 0.1)
    spins = []
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: spins.append(kwargs["timeout_sec"]))
    driver = SimpleNamespace(mode_status={"inspection_demo": {
        "active": True, "tree_running": True,
    }})
    module.Driver.dwell_mode(driver, "inspection_demo", 0.5)
    assert spins
    assert all(0 < timeout <= 0.2 for timeout in spins)


def _inspection_transition_driver():
    running = {"active": True, "tree_running": True, "_stamp": (20, 0)}
    driver = SimpleNamespace(
        mode_status={"inspection_demo": running},
        mode_status_receipts=[],
        _mode_status_floor={"inspection_demo": (10, 0), "reach_cable": (11, 0)},
        battery_status_sample=None,
        native_state_receipts=[],
        native_command_receipts=[],
        runtime_client=SimpleNamespace(),
    )
    for method_name in (
        "_inspection_auto_transition_evidence",
        "_inspection_success_since",
        "_inspection_battery_evidence",
        "wait_for_inspection_auto_transition",
        "wait_mode",
        "advance_inspection_to_reach",
    ):
        setattr(driver, method_name, getattr(module.Driver, method_name).__get__(driver))
    return driver, running


def _append_inspection_auto_receipts(driver, *, success=(21, 0), reach=(22, 0)):
    driver.mode_status_receipts.extend([
        {"mode": "inspection_demo", "stamp": success,
         "data": '{"active": false, "tree_finished": true, "tree_success": true}'},
        {"mode": "reach_cable", "stamp": reach,
         "data": '{"active": true, "tree_running": true}'},
    ])


def test_inspection_auto_recharge_during_dwell_requires_success_then_reach(monkeypatch):
    driver, running = _inspection_transition_driver()
    spins = 0

    def spin(*args, **kwargs):
        nonlocal spins
        spins += 1
        if spins == 1:
            _append_inspection_auto_receipts(driver)

    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", spin)
    driver.read_fresh_native_mission_phase = lambda: (
        "reach_cable", {"freshness": "fresh", "modes": [{"mode_key": "reach_cable", "active": True}]}
    )
    decision = module.Driver.advance_inspection_to_reach(driver, running, 1.0)
    assert decision.transition == "automatic"
    assert decision.inspection_status["tree_success"] is True
    assert decision.reach_status["active"] is True


def test_inspection_auto_recharge_race_after_recharge_rejection_is_verified(monkeypatch):
    driver, running = _inspection_transition_driver()
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    phases = iter(("inspection_demo", "reach_cable"))
    driver.read_fresh_native_mission_phase = lambda: (
        (phase := next(phases)),
        {"freshness": "fresh", "modes": [{"mode_key": phase, "active": True}]},
    )
    driver.native_flight_command = lambda command: (
        _append_inspection_auto_receipts(driver),
        (_ for _ in ()).throw(RuntimeError("intent raced automatic transition")),
    )
    decision = module.Driver.advance_inspection_to_reach(driver, running, 0.0)
    assert decision.transition == "automatic"


def test_inspection_success_waits_for_later_reach_and_runtime_api_convergence(monkeypatch):
    driver, running = _inspection_transition_driver()
    driver.mode_status_receipts.append({
        "mode": "inspection_demo", "stamp": (21, 0),
        "data": '{"active": false, "tree_finished": true, "tree_success": true}',
    })
    spins = 0

    def spin(*args, **kwargs):
        nonlocal spins
        spins += 1
        if spins == 1:
            _append_inspection_auto_receipts(driver, success=(21, 0), reach=(22, 0))

    phases = iter(("inspection_demo", "reach_cable"))
    driver.read_fresh_native_mission_phase = lambda: (
        (phase := next(phases)),
        {"freshness": "fresh", "modes": [{"mode_key": phase, "active": True}]},
    )
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", spin)
    decision = driver.advance_inspection_to_reach(running, 1.0)
    assert decision.transition == "automatic"
    assert decision.mission_state["modes"][0]["mode_key"] == "reach_cable"


def test_inspection_transition_retries_stale_runtime_api_before_reach(monkeypatch):
    driver, running = _inspection_transition_driver()
    _append_inspection_auto_receipts(driver)
    reads = 0

    def read_phase():
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError(
                "Cannot verify active mission phase from fresh Runtime API state: "
                "{'freshness': 'stale', 'modes': []}"
            )
        return "reach_cable", {
            "freshness": "fresh", "modes": [{"mode_key": "reach_cable", "active": True}]
        }

    driver.read_fresh_native_mission_phase = read_phase
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    decision = driver.wait_for_inspection_auto_transition(
        running, receipt_index=0, timeout_sec=0.5
    )
    assert decision is not None
    assert decision.transition == "automatic"
    assert reads >= 2


def test_inspection_transition_reports_persistent_runtime_api_nonconvergence(monkeypatch):
    driver, running = _inspection_transition_driver()
    _append_inspection_auto_receipts(driver)
    ticks = iter(index * 0.1 for index in range(100))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver.read_fresh_native_mission_phase = lambda: (_ for _ in ()).throw(RuntimeError(
        "Cannot verify active mission phase from fresh Runtime API state: "
        "{'freshness': 'stale', 'modes': []}"
    ))
    with pytest.raises(TimeoutError, match="ROS receipts prove.*Runtime API phase did not converge"):
        driver.wait_for_inspection_auto_transition(
            running, receipt_index=0, timeout_sec=0.5
        )


def test_inspection_transition_rejects_prior_generation_success_and_reach(monkeypatch):
    driver, running = _inspection_transition_driver()
    _append_inspection_auto_receipts(driver, success=(15, 0), reach=(16, 0))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    assert driver.wait_for_inspection_auto_transition(
        running, receipt_index=0, timeout_sec=0.0
    ) is None


def test_inspection_transition_fails_on_fresh_inspection_failure(monkeypatch):
    driver, running = _inspection_transition_driver()
    driver.mode_status_receipts.append({
        "mode": "inspection_demo", "stamp": (21, 0),
        "data": '{"active": false, "tree_finished": true, "tree_success": false}',
    })
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    with pytest.raises(RuntimeError, match="inspection_demo failed"):
        driver.wait_for_inspection_auto_transition(
            running, receipt_index=0, timeout_sec=1.0
        )


def test_inspection_manual_recharge_remains_available_when_no_auto_transition(monkeypatch):
    driver, running = _inspection_transition_driver()
    commands = []
    driver.mode_status["reach_cable"] = {"active": True, "tree_running": True}
    driver.read_fresh_native_mission_phase = lambda: (
        "inspection_demo", {"freshness": "fresh", "modes": [{"mode_key": "inspection_demo", "active": True}]}
    )
    driver.native_flight_command = lambda command: commands.append(command)
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver.wait_native_mission_phase = lambda mode, timeout_sec=15.0: {
        "freshness": "fresh", "modes": [{"mode_key": mode, "active": True}]
    }
    decision = driver.advance_inspection_to_reach(running, 0.0)
    assert decision.transition == "manual"
    assert commands == ["mission.recharge_now"]


def test_duration_guard_finishes_fresh_automatic_inspection_transition(monkeypatch):
    import time

    driver, running = _inspection_transition_driver()
    spins = 0

    def spin(*args, **kwargs):
        nonlocal spins
        spins += 1
        if spins == 1:
            _append_inspection_auto_receipts(driver)

    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", spin)
    driver.read_fresh_native_mission_phase = lambda: (
        "reach_cable", {"freshness": "fresh", "modes": [{"mode_key": "reach_cable", "active": True}]}
    )
    driver.monitor_inspection_until = module.Driver.monitor_inspection_until.__get__(driver)
    decision = driver.monitor_inspection_until(running, time.monotonic() + 2.0)
    assert decision is not None
    assert decision.transition == "automatic"


def test_duration_guard_ends_without_command_when_inspection_remains_healthy(monkeypatch):
    import time

    driver, running = _inspection_transition_driver()
    driver.read_fresh_native_mission_phase = lambda: pytest.fail(
        "healthy Inspection should not request or verify a recharge command"
    )
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver.monitor_inspection_until = module.Driver.monitor_inspection_until.__get__(driver)
    assert driver.monitor_inspection_until(running, time.monotonic() + 0.02) is None


@pytest.mark.parametrize("status,power,sim_state,expected", [
    (module.ChargerStatus.CHARGER_STATUS_CHARGING, 120.0, "latched=1", None),
    (module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED, 0.0, "latched=1", None),
])
def test_charging_dwell_requires_healthy_phase_and_positive_evidence(
    monkeypatch, status, power, sim_state, expected
):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    now = module.time.monotonic()
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={
            "charger_status": (now, status),
            "charging_power": (now, power),
            "sim_state": (now, sim_state),
        },
        battery_status_sample=None,
    )
    assert module.Driver.dwell_mode(
        driver, "cable_charging", 0.01, require_charging_evidence=True
    ) == []


@pytest.mark.parametrize("status,power,sim_state,reason", [
    (module.ChargerStatus.CHARGER_STATUS_CHARGING, 0.0, "latched=1", "invalid_charging_power"),
    (module.ChargerStatus.CHARGER_STATUS_CHARGING, 120.0, "latched=0", "unlatched"),
    (0, 120.0, "latched=1", "unsupported_charger_status"),
])
def test_brief_fresh_charging_predicate_failure_is_reported_and_recovered(
    monkeypatch, status, power, sim_state, reason
):
    import itertools
    ticks = itertools.count(0.0, 0.1)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={
            "charger_status": (0.0, module.ChargerStatus.CHARGER_STATUS_CHARGING),
            "charging_power": (0.0, 120.0),
            "sim_state": (0.0, "latched=1;conductor=cable"),
        },
        battery_status_sample=(0.0, SimpleNamespace(remaining=0.73, voltage_v=15.8)),
    )
    spins = 0
    def spin(*args, **kwargs):
        nonlocal spins
        spins += 1
        stamp = module.time.monotonic()
        sample = (
            status if spins == 1 else module.ChargerStatus.CHARGER_STATUS_CHARGING,
            power if spins == 1 else 120.0,
            sim_state if spins == 1 else "latched=1;conductor=cable",
        )
        driver.charging_samples = {
            "charger_status": (stamp, sample[0]),
            "charging_power": (stamp, sample[1]),
            "sim_state": (stamp, sample[2]),
        }
        driver.battery_status_sample = (
            stamp, SimpleNamespace(remaining=0.73, voltage_v=15.8)
        )
    monkeypatch.setattr(module, "spin_ready", spin)
    spans = module.Driver.dwell_mode(
        driver, "cable_charging", 0.3, require_charging_evidence=True
    )
    assert len(spans) == 1
    assert spans[0]["first"]["invalid_reasons"] == [reason]
    assert spans[0]["first"]["charger_status"] == status
    assert spans[0]["first"]["charging_power_w"] == power
    assert spans[0]["first"]["latched"] is ("latched=1" in sim_state)
    assert spans[0]["first"]["sim_state"] == sim_state
    assert spans[0]["first"]["battery_remaining"] == 0.73
    assert "charger_status" in spans[0]["first"]["source_ages_sec"]
    assert spans[0]["duration_sec"] <= 2.0


def test_sustained_fresh_charging_predicate_failure_raises_diagnostics(monkeypatch):
    import itertools
    ticks = itertools.count(0.0, 0.5)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={},
        battery_status_sample=None,
    )
    def spin(*args, **kwargs):
        stamp = module.time.monotonic()
        driver.charging_samples = {
            "charger_status": (stamp, 0),
            "charging_power": (stamp, 0.0),
            "sim_state": (stamp, "latched=1;conductor=cable"),
        }
        driver.battery_status_sample = (
            stamp, SimpleNamespace(remaining=0.7, voltage_v=15.5)
        )
    monkeypatch.setattr(module, "spin_ready", spin)
    with pytest.raises(RuntimeError, match="invalid beyond grace.*invalid_reasons.*source_ages_sec"):
        module.Driver.dwell_mode(
            driver, "cable_charging", 5.0, require_charging_evidence=True
        )


def test_charging_dwell_does_not_grace_mode_loss(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={},
        battery_status_sample=None,
    )
    def spin(*args, **kwargs):
        driver.mode_status["cable_charging"] = {"active": False, "tree_running": False}
    monkeypatch.setattr(module, "spin_ready", spin)
    with pytest.raises(RuntimeError, match="left its running phase"):
        module.Driver.dwell_mode(
            driver, "cable_charging", 1.0, require_charging_evidence=True
        )


def test_charging_dwell_rejects_stale_latched_full_evidence(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    now = module.time.monotonic()
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={
            "charger_status": (now - 3.0, module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (now - 3.0, 0.0),
            "sim_state": (now - 3.0, "latched=1;conductor=cable"),
        },
        battery_status_sample=None,
    )
    with pytest.raises(RuntimeError, match="stale during dwell"):
        module.Driver.dwell_mode(
            driver, "cable_charging", 0.01, require_charging_evidence=True
        )


@pytest.mark.parametrize("initial_phase", ["reach_cable", "cable_charging"])
def test_full_charge_wait_allows_bounded_charging_entry_handoff(monkeypatch, initial_phase):
    clock = [0.0]
    reads = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(mode_status={}, charging_samples={}, battery_status_sample=None)

    def spin(*args, **kwargs):
        clock[0] += 0.2
        driver.mode_status["cable_charging"] = {
            "active": True, "tree_running": clock[0] >= 0.4,
            "tree_finished": False, "tree_success": False,
        }
        driver.charging_samples = {
            "charger_status": (clock[0], module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (clock[0], 0.0),
            "sim_state": (clock[0], "latched=1;conductor=cable"),
        }
        driver.battery_status_sample = (clock[0], SimpleNamespace(remaining=1.0, voltage_v=16.8))

    def phase():
        value = initial_phase if clock[0] < 0.6 else "cable_charging"
        reads.append(value)
        return value, {}

    driver.read_fresh_native_mission_phase = phase
    monkeypatch.setattr(module, "spin_ready", spin)
    evidence = module.Driver.wait_until_fully_charged(driver, 20.0)
    assert clock[0] >= (0.6 if initial_phase == "reach_cable" else 0.4)
    assert evidence["mission_phase"] == "cable_charging"
    assert reads[0] == initial_phase


def test_full_charge_wait_tolerates_prior_success_flags_until_entry_is_running(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {
            "active": True, "tree_running": False,
            "tree_finished": True, "tree_success": True,
        }},
        charging_samples={},
        battery_status_sample=None,
    )
    phases = []

    def spin(*args, **kwargs):
        clock[0] += 0.2
        if clock[0] >= 0.6:
            driver.mode_status["cable_charging"] = {
                "active": True, "tree_running": True,
                "tree_finished": False, "tree_success": False,
            }
        driver.charging_samples = {
            "charger_status": (clock[0], module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (clock[0], 0.0),
            "sim_state": (clock[0], "latched=1;conductor=cable"),
        }
        driver.battery_status_sample = (
            clock[0], SimpleNamespace(remaining=1.0, voltage_v=16.8)
        )

    def phase():
        value = "reach_cable" if clock[0] < 0.8 else "cable_charging"
        phases.append(value)
        return value, {}

    driver.read_fresh_native_mission_phase = phase
    monkeypatch.setattr(module, "spin_ready", spin)
    evidence = module.Driver.wait_until_fully_charged(driver, 90.0)
    assert clock[0] >= 0.8
    assert evidence["mission_phase"] == "cable_charging"
    assert phases[0] == "reach_cable"


def test_prior_success_flags_never_bypass_bounded_entry_or_prove_full_charge(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {
            "active": True, "tree_running": False,
            "tree_finished": True, "tree_success": True,
        }},
        charging_samples={},
        battery_status_sample=None,
        read_fresh_native_mission_phase=lambda: ("reach_cable", {}),
    )

    def spin(*args, **kwargs):
        clock[0] += 0.2
        driver.charging_samples = {
            "charger_status": (clock[0], module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (clock[0], 0.0),
            "sim_state": (clock[0], "latched=1;conductor=cable"),
        }
        driver.battery_status_sample = (
            clock[0], SimpleNamespace(remaining=1.0, voltage_v=16.8)
        )

    monkeypatch.setattr(module, "spin_ready", spin)
    with pytest.raises(TimeoutError, match="Cable Charging entry did not converge within 10 seconds"):
        module.Driver.wait_until_fully_charged(driver, 90.0)
    assert 10.0 <= clock[0] <= 10.2


def test_failed_charging_tree_is_not_tolerated_by_entry_grace(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {
            "active": True, "tree_running": False,
            "tree_finished": True, "tree_success": False,
        }},
        charging_samples={},
        battery_status_sample=None,
        read_fresh_native_mission_phase=lambda: ("reach_cable", {}),
    )
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="Cable Charging failed"):
        module.Driver.wait_until_fully_charged(driver, 90.0)
    assert clock[0] == 0.0


@pytest.mark.parametrize("phase,expected", [
    ("reach_cable", TimeoutError), ("cable_charging", TimeoutError),
    ("custom_operation", RuntimeError),
])
def test_full_charge_entry_wait_is_bounded_and_rejects_unrelated_phase(monkeypatch, phase, expected):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": False}},
        charging_samples={}, battery_status_sample=None,
        read_fresh_native_mission_phase=lambda: (phase, {}),
    )

    def spin(*args, **kwargs):
        clock[0] += 0.2
        driver.charging_samples = {
            "charger_status": (clock[0], module.ChargerStatus.CHARGER_STATUS_CHARGING),
            "charging_power": (clock[0], 140.0),
            "sim_state": (clock[0], "latched=1;conductor=cable"),
        }
        driver.battery_status_sample = (clock[0], SimpleNamespace(remaining=0.9, voltage_v=16.5))

    monkeypatch.setattr(module, "spin_ready", spin)
    with pytest.raises(expected):
        module.Driver.wait_until_fully_charged(driver, 90.0)
    assert clock[0] <= 10.2
    if expected is TimeoutError:
        assert clock[0] >= 10.0
    else:
        assert clock[0] == 0.2


def test_full_charge_entry_grace_does_not_reopen_after_convergence(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={}, battery_status_sample=None,
        read_fresh_native_mission_phase=lambda: (
            "cable_charging" if clock[0] < 0.4 else "reach_cable", {}
        ),
    )

    def spin(*args, **kwargs):
        clock[0] += 0.2
        status = (module.ChargerStatus.CHARGER_STATUS_CHARGING if clock[0] < 0.4
                  else module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED)
        driver.charging_samples = {
            "charger_status": (clock[0], status),
            "charging_power": (clock[0], 140.0),
            "sim_state": (clock[0], "latched=1;conductor=cable"),
        }
        driver.battery_status_sample = (clock[0], SimpleNamespace(remaining=1.0, voltage_v=16.8))

    monkeypatch.setattr(module, "spin_ready", spin)
    with pytest.raises(RuntimeError, match="Unexpected mission phase"):
        module.Driver.wait_until_fully_charged(driver, 90.0)
    assert clock[0] == 0.4


def test_wait_until_fully_charged_accepts_fresh_latched_status_and_px4_battery(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    now = module.time.monotonic()
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        runtime_client=SimpleNamespace(_request=lambda *args: {
            "freshness": "fresh", "source_availability": "available",
            "modes": [{"active": True, "mode_key": "cable_charging", "freshness": "fresh"}],
        }),
        native_state_receipts=[],
        charging_samples={
            "charger_status": (now, module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (now, 0.0),
            "sim_state": (now, "latched=1;conductor=cable"),
        },
        battery_status_sample=(now, SimpleNamespace(remaining=0.99995, voltage_v=16.8)),
    )
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    evidence = module.Driver.wait_until_fully_charged(driver, 1.0)
    assert {key: value for key, value in evidence.items() if key != "mission_phase"} == {
        "charger_status": module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED,
        "charging_power_w": 0.0,
        "sim_state": "latched=1;conductor=cable",
        "battery_remaining": 0.99995,
        "battery_voltage_v": 16.8,
        "battery_age_sec": evidence["battery_age_sec"],
    }
    assert evidence["battery_age_sec"] <= 2.0


def test_wait_until_fully_charged_still_fails_on_publisher_silence(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    stale = module.time.monotonic() - 2.5
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        charging_samples={
            "charger_status": (stale, module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (stale, 0.0),
            "sim_state": (stale, "latched=1;conductor=cable"),
        },
        battery_status_sample=None,
    )
    with pytest.raises(RuntimeError, match="charging telemetry became stale"):
        module.Driver.wait_until_fully_charged(driver, 1.0)


@pytest.mark.parametrize("status,power,sim_state", [
    (module.ChargerStatus.CHARGER_STATUS_DISABLED, 0.0, "latched=1"),
    (module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED, 0.0, "latched=0"),
])
def test_wait_until_fully_charged_rejects_disabled_or_unlatched(status, power, sim_state, monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    now = module.time.monotonic()
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        runtime_client=SimpleNamespace(_request=lambda *args: {
            "freshness": "fresh", "source_availability": "available",
            "modes": [{"active": True, "mode_key": "cable_charging", "freshness": "fresh"}],
        }),
        native_state_receipts=[],
        charging_samples={
            "charger_status": (now, status),
            "charging_power": (now, power),
            "sim_state": (now, sim_state),
        },
        battery_status_sample=(now, SimpleNamespace(remaining=1.0, voltage_v=16.8)),
    )
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    with pytest.raises(RuntimeError, match="unsupported state|latch"):
        module.Driver.wait_until_fully_charged(driver, 1.0)


def test_wait_until_fully_charged_times_out_below_battery_threshold(monkeypatch):
    import itertools
    ticks = itertools.count(0.0, 0.1)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver = SimpleNamespace(
        mode_status={"cable_charging": {"active": True, "tree_running": True}},
        runtime_client=SimpleNamespace(_request=lambda *args: {
            "freshness": "fresh", "source_availability": "available",
            "modes": [{"active": True, "mode_key": "cable_charging", "freshness": "fresh"}],
        }),
        native_state_receipts=[],
        charging_samples={
            "charger_status": (0.0, module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (0.0, 0.0),
            "sim_state": (0.0, "latched=1;conductor=cable"),
        },
        battery_status_sample=(0.0, SimpleNamespace(remaining=0.97, voltage_v=16.8)),
    )
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    with pytest.raises(TimeoutError, match="did not reach fresh fully-charged"):
        module.Driver.wait_until_fully_charged(driver, 0.5)


def test_wait_until_fully_charged_accepts_automatic_leave_only_with_full_evidence(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    now = module.time.monotonic()
    driver = SimpleNamespace(
        mode_status={"cable_charging": {
            "active": False, "tree_running": False, "tree_finished": True, "tree_success": True,
        }},
        runtime_client=SimpleNamespace(_request=lambda *args: {
            "freshness": "fresh", "source_availability": "available",
            "modes": [{"active": True, "mode_key": "leave_cable", "freshness": "fresh"}],
        }),
        native_state_receipts=[],
        charging_samples={
            "charger_status": (now, module.ChargerStatus.CHARGER_STATUS_FULLY_CHARGED),
            "charging_power": (now, 0.0),
            "sim_state": (now, "latched=1;conductor=cable"),
        },
        battery_status_sample=(now, SimpleNamespace(remaining=0.99995, voltage_v=16.8)),
    )
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    evidence = module.Driver.wait_until_fully_charged(
        driver, 1.0, allow_leave_cable=True
    )
    assert evidence["mission_phase"] == "leave_cable"
    assert evidence["battery_remaining"] >= 0.98


def test_wait_until_fully_charged_does_not_infer_full_from_automatic_leave(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    now = module.time.monotonic()
    driver = SimpleNamespace(
        mode_status={"cable_charging": {
            "active": False, "tree_running": False, "tree_finished": True, "tree_success": True,
        }},
        runtime_client=SimpleNamespace(_request=lambda *args: {
            "freshness": "fresh", "source_availability": "available",
            "modes": [{"active": True, "mode_key": "leave_cable", "freshness": "fresh"}],
        }),
        native_state_receipts=[],
        charging_samples={
            "charger_status": (now, module.ChargerStatus.CHARGER_STATUS_CHARGING),
            "charging_power": (now, 140.0),
            "sim_state": (now, "latched=1;conductor=cable"),
        },
        battery_status_sample=(now, SimpleNamespace(remaining=0.95, voltage_v=16.0)),
    )
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    with pytest.raises(TimeoutError, match="did not reach fresh fully-charged"):
        module.Driver.wait_until_fully_charged(
            driver, 0.05, allow_leave_cable=True
        )


def _fresh_phase(mode_key):
    return {
        "freshness": "fresh",
        "source_availability": "available",
        "modes": [{"active": True, "mode_key": mode_key, "freshness": "fresh"}],
    }


def _fresh_leave_driver(initial_modes, command_response=None):
    phase_samples = iter(initial_modes)
    driver = SimpleNamespace(
        runtime_client=SimpleNamespace(
            _request=lambda *args: next(phase_samples),
            command=lambda *args: command_response,
        ),
        native_state_receipts=[],
        native_command_receipts=[],
        mode_status={"leave_cable": {"active": True, "tree_running": True, "_stamp": (100, 0)}},
        mode_status_receipts=[],
        _mode_status_floor={},
        wait_until_fully_charged=lambda *args, **kwargs: {"charger_status": 2, "battery_remaining": 0.99},
    )
    driver.native_flight_command = module.Driver.native_flight_command.__get__(driver)
    driver.wait_for_fresh_leave_cable_status = module.Driver.wait_for_fresh_leave_cable_status.__get__(driver)
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    driver.advance_after_charging = module.Driver.advance_after_charging.__get__(driver)
    driver._is_active_leave_phase_rejection = module.Driver._is_active_leave_phase_rejection
    return driver


def test_advance_after_charging_accepts_already_active_leave_without_command(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver = _fresh_leave_driver([_fresh_phase("leave_cable")])
    driver.native_flight_command = lambda *args, **kwargs: pytest.fail("leave was already active")
    result = module.Driver.advance_after_charging(
        driver, full_charge_evidence={"charger_status": 2, "battery_remaining": 0.99}
    )
    assert result["transition"] == "automatic"
    assert result["mission_state"]["modes"][0]["mode_key"] == "leave_cable"


def test_advance_after_charging_commands_leave_while_still_charging():
    driver = _fresh_leave_driver([
        _fresh_phase("cable_charging"),
    ], {"accepted": True, "request_id": "leave-request"})
    result = module.Driver.advance_after_charging(driver)
    assert result["transition"] == "commanded"
    assert driver.native_command_receipts == [{
        "command_id": "mission.leave_cable_now",
        "response": {"accepted": True, "request_id": "leave-request"},
    }]


def test_automatic_charging_waits_for_leave_without_command(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver = _fresh_leave_driver([
        _fresh_phase("cable_charging"), _fresh_phase("leave_cable"),
    ])
    driver.native_flight_command = lambda *args, **kwargs: pytest.fail("manual Leave forbidden")
    result = driver.advance_after_charging(
        full_charge_evidence={"charger_status": 2, "battery_remaining": 0.99},
        automatic_only=True,
    )
    assert result["transition"] == "automatic"
    assert driver.native_command_receipts == []


def test_automatic_charging_timeout_never_falls_back_to_manual(monkeypatch):
    import itertools
    ticks = itertools.count(0.0, 0.1)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver = _fresh_leave_driver(itertools.repeat(_fresh_phase("cable_charging")))
    driver.native_flight_command = lambda *args, **kwargs: pytest.fail("manual Leave forbidden")
    with pytest.raises(TimeoutError, match="automatic Leave"):
        driver.advance_after_charging(
            full_charge_evidence={"charger_status": 2, "battery_remaining": 0.99},
            automatic_only=True, transition_timeout_sec=0.2,
        )


def test_automatic_inspection_timeout_never_commands_recharge(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver, running = _inspection_transition_driver()
    driver.wait_for_inspection_auto_transition = lambda *args, **kwargs: None
    driver.native_flight_command = lambda *args, **kwargs: pytest.fail("manual recharge forbidden")
    with pytest.raises(TimeoutError, match="automatic recharge"):
        driver.advance_inspection_to_reach(running, 0.0, automatic_only=True)


def test_advance_after_charging_accepts_exact_forbidden_leave_race(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    response = {
        "accepted": False,
        "rejection": {
            "code": "forbidden",
            "message": "runtime intent service is not valid for active mode 'leave_cable'",
        },
    }
    driver = _fresh_leave_driver(
        [_fresh_phase("cable_charging"), _fresh_phase("leave_cable")], response
    )
    result = module.Driver.advance_after_charging(
        driver, full_charge_evidence={"charger_status": 2, "battery_remaining": 0.99}
    )
    assert result["transition"] == "automatic"
    assert len(driver.native_command_receipts) == 1


def test_advance_after_charging_propagates_other_forbidden_rejections():
    response = {
        "accepted": False,
        "rejection": {
            "code": "forbidden",
            "message": "runtime intent service is not valid for active mode 'inspection_demo'",
        },
    }
    driver = _fresh_leave_driver([_fresh_phase("cable_charging")], response)
    with pytest.raises(RuntimeError, match="inspection_demo"):
        module.Driver.advance_after_charging(driver)


def test_advance_after_charging_does_not_use_a_prior_rejection_receipt():
    old_response = {
        "accepted": False,
        "rejection": {
            "code": "forbidden",
            "message": "runtime intent service is not valid for active mode 'leave_cable'",
        },
    }
    driver = _fresh_leave_driver([_fresh_phase("cable_charging")])
    driver.native_command_receipts.append({
        "command_id": "mission.leave_cable_now", "response": old_response,
    })
    def fail_current_command(*args, **kwargs):
        raise RuntimeError("current command transport failed")
    driver.native_flight_command = fail_current_command
    with pytest.raises(RuntimeError, match="current command transport failed"):
        module.Driver.advance_after_charging(driver)


def test_wait_leave_success_accepts_fresh_completed_status_after_auto_leave(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver = SimpleNamespace(
        mode_status={"leave_cable": {
            "active": False, "tree_running": False,
            "tree_finished": True, "tree_success": True,
            "_stamp": (13, 0),
        }},
        mode_status_receipts=[
            # The activation may carry a stale terminal bit from the prior
            # lifecycle; it cannot satisfy this generation's success gate.
            {"mode": "leave_cable", "stamp": (11, 0), "data": '{"active": true, "tree_finished": true, "tree_success": true}'},
            {"mode": "leave_cable", "stamp": (12, 0), "data": '{"active": true, "tree_running": true, "tree_finished": false}'},
            {"mode": "leave_cable", "stamp": (13, 0), "data": '{"active": false, "tree_finished": true, "tree_success": true}'},
        ],
        _mode_status_floor={"leave_cable": (10, 0)},
    )
    result = module.Driver.wait_leave_cable_succeeded(driver)
    assert result["tree_finished"] is True
    assert result["tree_success"] is True


@pytest.mark.parametrize("mode", [
    {"active": False, "tree_running": False},
    {"active": True, "tree_running": False, "tree_finished": True, "tree_success": False},
])
def test_dwell_fails_if_phase_exits_or_fails(monkeypatch, mode):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver = SimpleNamespace(mode_status={"inspection_demo": mode})
    with pytest.raises(RuntimeError, match="inspection_demo"):
        module.Driver.dwell_mode(driver, "inspection_demo", 0.01)


@pytest.mark.parametrize("freshness,disagreement", [("stale", False), ("fresh", True)])
def test_native_state_wait_rejects_stale_or_conflicting_arm_evidence(monkeypatch, freshness, disagreement):
    import itertools
    ticks = itertools.count(0.0, 0.1)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver = SimpleNamespace(
        native_state_receipts=[],
        runtime_client=SimpleNamespace(vehicle_status=lambda: {
            "armed": True, "freshness": "fresh",
            "latest": {"dangerous_commands_allowed": True},
            "telemetry_fields": {"armed": {
                "value": True, "freshness": freshness,
                "source_availability": "available", "disagreement": disagreement,
            }},
        }),
    )
    with pytest.raises(TimeoutError, match="did not converge"):
        module.Driver.wait_native_vehicle_state(driver, {"armed": True}, timeout_sec=0.5)
    assert driver.native_state_receipts == []


def test_native_cleanup_land_rejection_propagates_after_hold(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver, commands = _cleanup_driver(armed=True)
    def reject_land(command, parameters=None):
        commands.append(command)
        if command == "px4.land":
            raise RuntimeError("Native land rejected")
    driver.native_flight_command = reject_land
    with pytest.raises(RuntimeError, match="Native land rejected"):
        module.Driver.land_and_wait(driver, None)
    assert commands == ["px4.hold", "px4.land"]


def test_native_cleanup_waits_out_transient_navigation_disagreement(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver, commands = _cleanup_driver(armed=True)
    verified = []
    driver.wait_native_vehicle_state = lambda expected, **kwargs: verified.append(expected)
    module.Driver.land_and_wait(driver, None)
    assert commands == ["px4.hold", "px4.land"]
    assert verified == [{"armed": True, "in_air": True}]


def _cleanup_driver(*, armed=False, owner_clears=True, owner_active=True, hold_accepted=True):
    status = SimpleNamespace(arming_state=(
        module.VehicleStatus.ARMING_STATE_ARMED if armed
        else module.VehicleStatus.ARMING_STATE_DISARMED
    ))
    land = SimpleNamespace(landed=not armed)
    commands = []
    control_reads = 0
    mission_reads = 0

    def command(command_id, parameters=None):
        commands.append(command_id)
        if command_id == "px4.hold" and not hold_accepted:
            raise RuntimeError("Native px4.hold rejected")
        if command_id == "px4.land":
            status.arming_state = module.VehicleStatus.ARMING_STATE_DISARMED
            land.landed = True
        return {"accepted": True}

    def request(method, path):
        nonlocal control_reads, mission_reads
        if path == "/control/status":
            control_reads += 1
            clear = not owner_active or (owner_clears and control_reads >= 2)
            return {
                "freshness": "fresh", "source_availability": "available",
                "owner": "px4_hold" if clear else "mission",
                "active_setpoint_owner": "px4" if clear else "mission_executor",
                "latest": {
                    "hold_interruption": {
                        "command_id": "px4.hold", "completed": clear,
                        "interrupted_owners": ["mission"],
                    },
                    "transition": {
                        "command_id": "px4.hold", "status": "terminated",
                        "message": "PX4 Hold confirmed; autonomous action stopped",
                    } if clear else {"command_id": "px4.hold", "status": "stopping"},
                    "mission_hold_termination": {
                    "completed": clear, "interrupted_owners": ["mission"],
                    },
                },
            }
        if path == "/mission/status":
            mission_reads += 1
            clear = not owner_active or (owner_clears and mission_reads >= 2)
            return {
                "freshness": "fresh", "source_availability": "available",
                "latest": {"mission_active": not clear},
                "modes": [{"mode_key": "cable_charging", "active": not clear}],
            }
        raise AssertionError(path)

    runtime_client = SimpleNamespace(
        _request=request,
        vehicle_status=lambda: {
            "freshness": "fresh", "armed": status.arming_state == module.VehicleStatus.ARMING_STATE_ARMED,
            "telemetry_fields": {"armed": {
                "value": status.arming_state == module.VehicleStatus.ARMING_STATE_ARMED,
                "freshness": "fresh", "source_availability": "available",
                "disagreement": False,
            }},
        },
    )
    driver = SimpleNamespace(
        runtime_client=runtime_client,
        native_state_receipts=[],
        native_command_receipts=[],
        vehicle=status,
        land_detected=land,
        wait_vehicle=lambda **kwargs: status,
        native_flight_command=command,
        _runtime_api_call=lambda operation, **kwargs: operation(),
    )
    driver.wait_native_mission_owner_cleared = lambda timeout_sec=15.0: (
        module.Driver.wait_native_mission_owner_cleared(driver, timeout_sec)
    )
    driver.wait_native_disarmed = lambda timeout_sec=15.0: (
        module.Driver.wait_native_disarmed(driver, timeout_sec)
    )
    driver.wait_native_vehicle_state = lambda expected, timeout_sec=15.0: (
        {"freshness": "fresh", **expected}
    )
    return driver, commands


def test_disarmed_cleanup_holds_until_cable_charging_owner_is_cleared(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver, commands = _cleanup_driver(armed=False)
    module.Driver.land_and_wait(driver, None, timeout_sec=1.0)
    assert commands == ["px4.hold"]
    assert driver.native_state_receipts[0]["expected_control_owner"] == "px4_hold"
    assert driver.native_state_receipts[-1]["expected"] == {"armed": False}


def test_already_grounded_without_autonomous_owner_still_confirms_hold(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver, commands = _cleanup_driver(armed=False, owner_active=False)
    module.Driver.land_and_wait(driver, None, timeout_sec=1.0)
    assert commands == ["px4.hold"]


def test_airborne_cleanup_holds_then_lands_and_never_arms(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver, commands = _cleanup_driver(armed=True)
    module.Driver.land_and_wait(driver, None, timeout_sec=1.0)
    assert commands == ["px4.hold", "px4.land"]


def test_armed_grounded_cleanup_reports_native_disarm_limitation_without_waiting(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    driver, commands = _cleanup_driver(armed=True)
    original_status = driver.runtime_client.vehicle_status
    def grounded_status():
        value = original_status()
        value["in_air"] = False
        value["telemetry_fields"]["in_air"] = {
            "value": False, "freshness": "fresh",
            "source_availability": "available", "disagreement": False,
        }
        return value
    driver.runtime_client.vehicle_status = grounded_status
    with pytest.raises(RuntimeError, match="PX4 remains armed on the ground"):
        module.Driver.land_and_wait(driver, None, timeout_sec=1.0)
    assert commands == ["px4.hold"]


def test_cleanup_hold_rejection_prevents_claiming_safe_state():
    driver, commands = _cleanup_driver(hold_accepted=False)
    with pytest.raises(RuntimeError, match="Native px4.hold rejected"):
        module.Driver.land_and_wait(driver, None, timeout_sec=1.0)
    assert commands == ["px4.hold"]


def test_cleanup_owner_timeout_prevents_land_and_safe_return(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    driver, commands = _cleanup_driver(armed=True, owner_clears=False)
    with pytest.raises(TimeoutError, match="cleared mission modes"):
        module.Driver.land_and_wait(driver, None, timeout_sec=0.1)
    assert commands == ["px4.hold"]


@pytest.mark.parametrize("hold_ready", [True, False])
def test_native_inspection_activation_requires_confirmed_hold_handoff(hold_ready):
    sequence = []
    def wait_native(expected):
        sequence.append(("native_state", expected))
        if not hold_ready:
            raise TimeoutError("Hold state has not converged")
    driver = SimpleNamespace(
        native_flight_command=lambda command, params=None: sequence.append((command, params)),
        wait_nav_state=lambda state: sequence.append(("ros_nav", state)),
        wait_native_vehicle_state=wait_native,
    )
    if hold_ready:
        module.Driver.activate_inspection_from_ingress(driver, 26)
    else:
        with pytest.raises(TimeoutError, match="Hold state"):
            module.Driver.activate_inspection_from_ingress(driver, 26)
    assert sequence[:3] == [
        ("px4.hold", None),
        ("ros_nav", module.VehicleStatus.NAVIGATION_STATE_AUTO_LOITER),
        ("native_state", {"armed": True, "in_air": True, "nav_state": "hold"}),
    ]
    assert sequence[3:] == ([
        ("mission.activate", {"mode_key": "inspection_demo"}), ("ros_nav", 26),
    ] if hold_ready else [])


@pytest.mark.parametrize("invalid", ["predecessor", "stale", "ambiguous"])
def test_native_mission_phase_waits_for_gate_consumer(monkeypatch, invalid):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    matching = {"mode_key": "cable_charging", "active": True, "freshness": "fresh"}
    previous = {"mode_key": "reach_cable", "active": True, "freshness": "fresh"}
    bad = {"freshness": "fresh", "source_availability": "available", "modes": [matching]}
    if invalid == "predecessor":
        bad["modes"] = [previous]
    elif invalid == "stale":
        bad["freshness"] = "stale"
    else:
        bad["modes"] = [previous, matching]
    good = {"freshness": "fresh", "source_availability": "available", "modes": [matching]}
    samples = iter([bad, bad, good])
    calls = []
    def request(method, path):
        calls.append((method, path))
        return next(samples)
    driver = SimpleNamespace(runtime_client=SimpleNamespace(_request=request), native_state_receipts=[])
    assert module.Driver.wait_native_mission_phase(driver, "cable_charging") == good
    assert len(calls) == 3
    assert driver.native_state_receipts == [{"expected_mission_phase": "cable_charging", "state": good}]


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "rejected", "cancelled"])
def test_native_terminal_wait_matches_request_and_rejects_failures(monkeypatch, terminal):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    def event(request, status):
        return {"category": "command_result", "request_id": request, "details": {"status": status}}
    samples = iter([[event("old", "succeeded")], [event("current", terminal)]])
    driver = SimpleNamespace(runtime_client=SimpleNamespace(_request=lambda *args: next(samples)), native_state_receipts=[])
    if terminal == "succeeded":
        assert module.Driver.wait_native_command_result(driver, "current")["status"] == "succeeded"
        assert len(driver.native_state_receipts) == 1
    else:
        with pytest.raises(RuntimeError, match=f"ended {terminal}"):
            module.Driver.wait_native_command_result(driver, "current")
        assert driver.native_state_receipts == []


def test_interrupt_keeps_ros_available_for_driver_cleanup():
    import subprocess
    import sys
    # Exercise the actual ROS signal installation in a separate process so
    # an interrupt cannot contaminate the test runner's shared context.
    result = subprocess.run(
        [sys.executable, "-c", """
import signal
import rclpy
from hil_inspection_cycle_driver import initialize_driver_ros
# Background shells may inherit SIG_IGN; the driver must own its interrupt.
signal.signal(signal.SIGINT, signal.SIG_IGN)
initialize_driver_ros()
try:
    signal.raise_signal(signal.SIGINT)
except KeyboardInterrupt:
    assert rclpy.ok(), 'interrupt shut down ROS before landing cleanup'
else:
    raise AssertionError('interrupt did not unwind to cleanup')
rclpy.shutdown()
"""],
        cwd=Path(__file__).parent, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_spin_ready_services_later_subscriptions_under_backlog():
    """A busy early subscription must not starve a later one (charger topics)."""
    import os
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile
    from std_msgs.msg import Int32

    os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    context = module.rclpy.context.Context()
    module.rclpy.init(context=context, domain_id=91)
    node = module.rclpy.create_node("spin_ready_fairness", context=context)
    try:
        received = {"busy": 0, "late": 0}
        qos = QoSProfile(depth=100)
        node.create_subscription(Int32, "busy", lambda _: received.__setitem__("busy", received["busy"] + 1), qos)
        node.create_subscription(Int32, "late", lambda _: received.__setitem__("late", received["late"] + 1), qos)
        busy_pub = node.create_publisher(Int32, "busy", qos)
        late_pub = node.create_publisher(Int32, "late", qos)
        fake = SimpleNamespace(_executor=SingleThreadedExecutor(context=context), _SPIN_DRAIN_LIMIT=256)
        fake._executor.add_node(node)
        deadline = time.monotonic() + 5.0
        while (busy_pub.get_subscription_count() == 0 or late_pub.get_subscription_count() == 0) \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        for value in range(50):
            busy_pub.publish(Int32(data=value))
        late_pub.publish(Int32(data=1))
        time.sleep(0.3)
        # One pump must reach the later subscription despite the busy backlog.
        module.spin_ready(fake, timeout_sec=0.2)
        assert received["late"] == 1
        assert received["busy"] >= 1
        fake._executor.shutdown(timeout_sec=0.0)
    finally:
        node.destroy_node()
        module.rclpy.shutdown(context=context)



def _phase_driver(responses):
    replies = iter(responses)
    driver = SimpleNamespace(
        runtime_client=SimpleNamespace(_request=lambda *args: next(replies)),
        native_state_receipts=[],
    )
    driver.read_fresh_native_mission_phase = module.Driver.read_fresh_native_mission_phase.__get__(driver)
    return driver


def test_mission_phase_read_settles_through_an_automatic_handoff(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    handoff = {"freshness": "fresh", "source_availability": "available", "modes": [
        {"active": False, "mode_key": "cable_charging", "freshness": "fresh"},
        {"active": False, "mode_key": "leave_cable", "freshness": "fresh"},
    ]}
    settled = {"freshness": "fresh", "source_availability": "available", "modes": [
        {"active": True, "mode_key": "leave_cable", "freshness": "fresh"},
    ]}
    driver = _phase_driver([handoff, handoff, settled])
    phase, state = driver.read_fresh_native_mission_phase()
    assert phase == "leave_cable" and state is settled


def test_mission_phase_read_still_fails_when_it_never_settles(monkeypatch):
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    stale = {"freshness": "stale", "source_availability": "available", "modes": []}
    driver = _phase_driver(iter(lambda: stale, None))
    with pytest.raises(RuntimeError, match="Cannot verify active mission phase"):
        driver.read_fresh_native_mission_phase(settle_timeout_sec=0.05)


def test_mission_trees_stopped_requires_every_mode_inactive_and_idle():
    idle = {key: {"active": False, "tree_running": False} for key in module.MISSION_MODE_KEYS}
    assert module.mission_trees_stopped(idle)
    assert module.mission_trees_stopped({})
    running = dict(idle, reach_cable={"active": False, "tree_running": True})
    assert not module.mission_trees_stopped(running)


def _hold_scenario_driver(monkeypatch, mode_status_after_hold, nav_states):
    import itertools
    ticks = itertools.count(0.0, 0.5)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    nav = iter(nav_states)
    commands = []
    driver = SimpleNamespace(
        mode_status={"inspection_demo": {"active": True, "tree_running": True}},
        vehicle=None,
    )
    def command(command_id):
        commands.append(command_id)
        driver.mode_status = dict(mode_status_after_hold)
    driver.native_flight_command = command
    driver.wait_mode = lambda key, predicate, **kwargs: driver.mode_status[key]
    driver.wait_native_mission_owner_cleared = lambda timeout_sec: {"proof": True}
    def spin(*args, **kwargs):
        if commands:
            driver.vehicle = SimpleNamespace(nav_state=next(nav, module.VehicleStatus.NAVIGATION_STATE_AUTO_LOITER),
                                             failsafe=False)
    monkeypatch.setattr(module, "spin_ready", spin)
    args = SimpleNamespace(operator_hold_phase="inspection_demo", operator_hold_after_sec=1.0,
                           handover_observe_sec=2.0, automatic_recharge_timeout_sec=600.0,
                           charge_until_full_timeout_sec=90.0)
    return driver, args, commands


def test_operator_hold_scenario_records_a_clean_handover(monkeypatch):
    idle = {key: {"active": False, "tree_running": False} for key in module.MISSION_MODE_KEYS}
    driver, args, commands = _hold_scenario_driver(monkeypatch, idle, [])
    events = []
    module.run_operator_hold_scenario(driver, args, events.append)
    assert commands == ["px4.hold"]
    assert [e["event"] for e in events] == ["operator_hold_commanded", "operator_hold_handover_verified"]


def test_operator_hold_scenario_fails_when_a_mode_reactivates(monkeypatch):
    still_running = {"inspection_demo": {"active": False, "tree_running": True}}
    driver, args, _ = _hold_scenario_driver(monkeypatch, still_running, [])
    with pytest.raises(RuntimeError, match="still running after Hold"):
        module.run_operator_hold_scenario(driver, args, lambda event: None)


def test_operator_hold_scenario_fails_when_px4_leaves_hold(monkeypatch):
    idle = {key: {"active": False, "tree_running": False} for key in module.MISSION_MODE_KEYS}
    driver, args, _ = _hold_scenario_driver(
        monkeypatch, idle, [module.VehicleStatus.NAVIGATION_STATE_AUTO_LOITER,
                            module.VehicleStatus.NAVIGATION_STATE_AUTO_LAND])
    with pytest.raises(RuntimeError, match="left Hold"):
        module.run_operator_hold_scenario(driver, args, lambda event: None)



def test_operator_hold_scenario_fast_forwards_with_intent_services(monkeypatch):
    idle = {key: {"active": False, "tree_running": False} for key in module.MISSION_MODE_KEYS}
    driver, args, commands = _hold_scenario_driver(monkeypatch, idle, [])
    args.operator_hold_phase = "leave_cable"
    driver.mode_status = {key: {"active": True, "tree_running": True} for key in module.MISSION_MODE_KEYS}
    calls = []
    def reach(status, dwell, stop_at=None, automatic_only=False):
        calls.append(("reach", dwell, automatic_only))
        return SimpleNamespace(transition="commanded")
    driver.advance_inspection_to_reach = reach
    driver.wait_charging_evidence = lambda: {"charging_power_w": 150.0}
    driver.wait_native_mission_phase = lambda key: calls.append(("phase", key))
    driver.advance_after_charging = lambda automatic_only=False: (
        calls.append(("leave", automatic_only)) or {"transition": "commanded"})
    events = []
    module.run_operator_hold_scenario(driver, args, events.append)
    assert calls == [("reach", 0.0, False), ("phase", "cable_charging"), ("leave", False)]
    names = [e["event"] for e in events]
    assert names[:3] == ["reach_cable_active", "charging_power_verified", "leave_cable_commanded"]
    assert names[-1] == "operator_hold_handover_verified" and commands == ["px4.hold"]


def test_phase_read_survives_one_slow_read_inside_a_mode_handoff(monkeypatch):
    # HIL soak run 16: the only read inside the 0.5 s Cable Charging -> Leave
    # Cable handoff showed both modes active and took ~3 s to return.
    handoff = {"freshness": "fresh", "source_availability": "available", "modes": [
        {"mode_key": "cable_charging", "active": True, "freshness": "fresh"},
        {"mode_key": "leave_cable", "active": True, "freshness": "fresh"}]}
    settled = {"freshness": "fresh", "source_availability": "available", "modes": [
        {"mode_key": "cable_charging", "active": False, "freshness": "fresh"},
        {"mode_key": "leave_cable", "active": True, "freshness": "fresh"}]}
    now = [0.0]
    responses = [handoff, settled]

    def request(method, path):
        assert (method, path) == ("GET", "/mission/status")
        response = responses.pop(0)
        if response is handoff:
            now[0] += 3.5
        return response

    driver = SimpleNamespace(runtime_client=SimpleNamespace(_request=request), native_state_receipts=[])
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    phase, state = module.Driver.read_fresh_native_mission_phase(driver)
    assert phase == "leave_cable"
    assert state is settled


def test_phase_read_still_fails_when_two_modes_stay_active(monkeypatch):
    stuck = {"freshness": "fresh", "source_availability": "available", "modes": [
        {"mode_key": "cable_charging", "active": True, "freshness": "fresh"},
        {"mode_key": "leave_cable", "active": True, "freshness": "fresh"}]}
    now = [0.0]

    def request(method, path):
        now[0] += 1.5
        return stuck

    driver = SimpleNamespace(runtime_client=SimpleNamespace(_request=request), native_state_receipts=[])
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.rclpy, "ok", lambda: True)
    monkeypatch.setattr(module, "spin_ready", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="Cannot verify active mission phase"):
        module.Driver.read_fresh_native_mission_phase(driver)
