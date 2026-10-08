"""REALTIME_OVERLOAD_v1 bookkeeping, node-configuration checks and pinning (no ROS graph, no estimator)."""

import os

import pytest

from iii_drone_powerline_slam import realtime

LIMITS = {"contract": realtime.CONTRACT, "live_age_budget_s": 2.0, "startup_timeout_s": 10.0,
          "max_unprocessed_events": 100, "max_node_queue": 50}
RT_AFFINITY = {"node": [1], "host": [2, 3], "detector_workers": [4, 5], "mask_worker": [6], "radar_workers": [6],
               "doppler_workers": [7]}


def _armed() -> realtime.OverloadMonitor:
    monitor = realtime.OverloadMonitor(LIMITS)
    monitor.received("camera", 1_000, 100.0)
    assert monitor.frame(0.3, has_lines=False) is None and monitor.armed
    return monitor


# ------------------------------------------------------------------------------------------------- configuration
def test_an_empty_node_configuration_is_the_reference_path():
    realtime.validate_node_config({})
    realtime.validate_node_config({"pipeline": "rt", "intake": "waitset", "mask_device": "cuda", "overload": dict(LIMITS),
                                   "affinity": dict(RT_AFFINITY)})


@pytest.mark.parametrize("node", [
    {"pipeline": "fast"}, {"intake": "polling"}, {"mask_device": "gpu"},
    {"affinity": {"gpu": [1]}}, {"affinity": {"node": []}}, {"affinity": {"node": [1.5]}},
    {"overload": {k: v for k, v in LIMITS.items() if k != "max_node_queue"}},
    {"overload": dict(LIMITS, live_age_budget_s=0)}, {"overload": dict(LIMITS, contract="OTHER")},
    {"overload": dict(LIMITS, extra=1)},
])
def test_unusable_node_configurations_are_refused(node):
    with pytest.raises(ValueError):
        realtime.validate_node_config(node)


def test_fusion30_is_selectable_only_on_the_realtime_pipeline():
    realtime.validate_node_config({"pipeline": "rt", "fusion": "fusion30"})
    realtime.validate_node_config({"pipeline": "rt", "fusion": "exact"})
    realtime.validate_node_config({"fusion": "exact"})
    realtime.validate_node_config({"fusion": "fusion30"})                   # the real-time pipeline is the only live pipeline
    realtime.validate_node_config({"fusion": "cycle30"})                    # CYCLE_FUSION30_v2
    with pytest.raises(ValueError, match="node.fusion"):
        realtime.validate_node_config({"pipeline": "rt", "fusion": "fusion60"})


def test_gpu_mask_authority_admits_no_configuration_with_a_cpu_mask_network():
    """GPU_MASK_AUTHORITY_v1: nothing a node configuration can say brings CPU inference into the live runtime."""
    realtime.validate_node_config({"pipeline": "rt", "mask_device": "cuda", "mask_workers": 1, "mask_fallback_workers": 0})
    for node in ({"pipeline": "r1"}, {"mask_device": "cpu"}, {"mask_fallback_workers": 2}, {"mask_fallback_workers": 4, "mask_device": "cuda"},
                 {"prefetch_workers": 6}, {"mask_workers": 2}):
        with pytest.raises(ValueError, match="GPU_MASK_AUTHORITY_v1"):
            realtime.validate_node_config(node)


def test_fast_traversal_epoch_v2_configuration():
    base = {"pipeline": "rt", "mission_prior": "live"}
    realtime.validate_node_config(dict(base, rollover={"contract": "FAST_TRAVERSAL_EPOCH_v2"}))
    realtime.validate_node_config(dict(base, rollover={"contract": "FAST_TRAVERSAL_EPOCH_v2", "record_dir": "/tmp/x",
                                                       "seal_grace_s": 0.3, "final_timeout_s": 5.0}))
    realtime.validate_node_config(dict(base, rollover={"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": 60.0}))
    for bad in ({"contract": "FAST_TRAVERSAL_EPOCH_v2", "timeout_s": 60.0},          # a v1 key
                {"contract": "FAST_TRAVERSAL_EPOCH_v2", "seal_grace_s": 0},
                {"contract": "FAST_TRAVERSAL_EPOCH_v2", "final_timeout_s": -1},
                {"contract": "FAST_TRAVERSAL_EPOCH_v3"}):
        with pytest.raises(ValueError):
            realtime.validate_node_config(dict(base, rollover=bad))
    with pytest.raises(ValueError, match="GPU_MASK_AUTHORITY_v1"):         # the CPU reference pipeline has no warm runtime (and is not live)
        realtime.validate_node_config({"pipeline": "r1", "mission_prior": "live", "rollover": {"contract": "FAST_TRAVERSAL_EPOCH_v2"}})
    assert realtime.RecoveryLedger.recoverable("FAST_TRAVERSAL_EPOCH_v2:ROLLOVER_FAILED: x") if hasattr(realtime.RecoveryLedger, "recoverable") else True


def test_a_partial_affinity_plan_is_refused_because_children_inherit_their_parents_cpus():
    with pytest.raises(ValueError, match="missing"):
        realtime.validate_node_config({"pipeline": "rt", "affinity": {"node": [1], "host": [2]}})
    with pytest.raises(ValueError, match="prefetch_workers"):
        realtime.validate_node_config({"prefetch_workers": 6, "affinity": {"node": [1], "host": [2]}})
    with pytest.raises(ValueError, match="mask_fallback_workers"):
        realtime.validate_node_config({"pipeline": "rt", "doppler_workers": 2, "mask_fallback_workers": 2,
                                       "affinity": dict(RT_AFFINITY)})
    # a role without processes needs no CPUs
    realtime.validate_node_config({"pipeline": "rt", "radar_workers": 0,
                                   "affinity": {k: v for k, v in RT_AFFINITY.items() if k not in ("radar_workers", "doppler_workers")}})


def test_pinning_sets_every_thread_and_reports_the_cpus():
    allowed = sorted(os.sched_getaffinity(0))
    assert realtime.pin_process(os.getpid(), None) is None
    assert realtime.pin_process(os.getpid(), allowed[:1]) == allowed[:1]
    assert os.sched_getaffinity(0) == set(allowed[:1])
    realtime.pin_process(os.getpid(), allowed)
    assert os.sched_getaffinity(0) == set(allowed)


# ------------------------------------------------------------------------------------------------------- overload
def test_startup_tolerates_stale_empty_frames_but_never_stale_lines():
    monitor = realtime.OverloadMonitor(LIMITS)
    monitor.received("radar_u", 1_000, 100.0)
    assert monitor.frame(4.5, has_lines=False) is None and not monitor.armed
    assert monitor.status()["frames"]["startup_stale_empty"] == 1
    assert monitor.frame(4.5, has_lines=True) == "OUTPUT_AGE"
    assert monitor.record("OUTPUT_AGE")["fail_closed_reason"] == "REALTIME_OVERLOAD_v1:OUTPUT_AGE"
    assert monitor.record("OUTPUT_AGE")["trigger"] == {"code": "OUTPUT_AGE", "output_age_s": 4.5, "armed": False,
                                                       "has_lines": True}


def test_the_first_in_budget_frame_arms_and_then_every_stale_frame_trips():
    monitor = _armed()
    assert monitor.frame(1.9, has_lines=True) is None
    assert monitor.frame(2.1, has_lines=False) == "OUTPUT_AGE"
    assert monitor.frame(0.1, has_lines=True) is None and monitor.code == "OUTPUT_AGE"     # latched: nothing is re-armed


def test_input_age_counts_only_events_that_processing_holds_back():
    monitor = _armed()
    monitor.received("odometry", 2_000, 101.0)
    monitor.received("radar_u", 1_500, 101.2)
    assert monitor.head() == (1_000, 100.0)
    monitor.processed(1_000)
    assert monitor.head() == (1_500, 101.2)                       # the next event by source time, not by arrival
    # starved (its watermarks are not satisfied): no age, however long it waits
    assert monitor.check(110.0, head_ready=False, unprocessed_events=2) is None
    assert monitor.check(103.1, head_ready=True, unprocessed_events=2) is None
    assert monitor.check(103.3, head_ready=True, unprocessed_events=2) == "INPUT_AGE"
    assert monitor.status()["code"] == "INPUT_AGE"
    assert monitor.check(120.0, head_ready=True, unprocessed_events=10_000) is None      # one trigger per epoch


def test_queue_depth_is_bounded_in_every_phase():
    monitor = realtime.OverloadMonitor(LIMITS)
    assert monitor.check(0.0, head_ready=False, unprocessed_events=100) is None
    assert monitor.check(0.0, head_ready=False, unprocessed_events=101) == "QUEUE_DEPTH"
    assert monitor.status()["high_water"]["unprocessed_events"] == 101


def test_an_epoch_that_never_arms_times_out_after_its_first_input():
    monitor = realtime.OverloadMonitor(LIMITS)
    assert monitor.check(1_000.0, head_ready=False, unprocessed_events=0) is None       # no input yet: waiting, not late
    monitor.received("imu", 5, 50.0)
    assert monitor.check(59.9, head_ready=False, unprocessed_events=1) is None
    assert monitor.check(60.1, head_ready=False, unprocessed_events=1) == "STARTUP_TIMEOUT"
    armed = _armed()
    assert armed.check(1_000.0, head_ready=False, unprocessed_events=0) is None          # armed epochs have no deadline


def test_the_record_names_limits_trigger_and_recovery():
    monitor = _armed()
    monitor.processed(1_000)
    monitor.received("camera", 3_000, 200.0)
    assert monitor.check(203.0, head_ready=True, unprocessed_events=7) == "INPUT_AGE"
    record = monitor.record("INPUT_AGE")
    assert record["kind"] == "overload" and record["contract"] == realtime.CONTRACT
    assert record["limits"] == {k: v for k, v in LIMITS.items() if k != "contract"}
    assert record["trigger"]["input_age_s"] == 3.0 and record["trigger"]["head_source_time_ns"] == 3_000
    assert record["status"]["arming_frame_age_s"] == 0.3
    assert "fresh processing epoch" in record["recovery"]


def test_host_gc_policy_runs_full_collections_only_when_asked():
    """HOST_GC_AT_SEAL_v1: after begin() no automatic full collection, cyclic garbage goes at the chosen collection."""
    import gc

    from iii_drone_powerline_slam import estimator_host

    before = gc.get_threshold()
    policy = estimator_host.HostGc()
    try:
        policy.begin()
        assert gc.get_threshold()[:2] == before[:2] and gc.get_threshold()[2] >= 1 << 30
        assert policy.status["frozen"] > 0

        class Node:
            pass

        keep = []
        for _ in range(200_000):                            # far past every automatic threshold
            a, b = Node(), Node()
            a.other, b.other = b, a
            keep.append(a)
        assert policy.status["automatic_full_collections"] == 0
        del keep, a, b
        policy.collect()
        assert policy.status["full_collections"] == 1 and policy.status["collected"] >= 400_000
        assert policy.status["automatic_full_collections"] == 0 and policy.status["last_s"] is not None
    finally:
        gc.callbacks.remove(policy._callback)
        gc.unfreeze()
        gc.set_threshold(*before)


def test_host_gc_bounded_pass_walks_only_what_is_new_and_frozen_cycles_go_at_the_chosen_collection(monkeypatch):
    """HOST_GC_BOUNDED_v2: a due pass collects the cycles made since the last one and freezes the survivors; a later pass
    does not walk them again; survivors that become garbage afterwards are taken by the chosen full collection."""
    import gc

    from iii_drone_powerline_slam import estimator_host

    before = gc.get_threshold()
    monkeypatch.setattr(estimator_host, "GC_PASS_INTERVAL_S", 0.0)
    policy = estimator_host.HostGc()
    try:
        policy.begin()
        frozen_at_start = policy.status["frozen"]

        class Node:
            pass

        def ring(count):
            out = []
            for _ in range(count):
                a, b = Node(), Node()
                a.other, b.other = b, a
                out.append(a)
            return out

        keep = ring(50_000)
        garbage = ring(50_000)
        del garbage
        assert policy.bounded_pass(idle=True) is not None
        assert policy.status["bounded_passes"] == 1 and policy.status["bounded_collected"] >= 100_000
        assert policy.status["frozen"] >= frozen_at_start + 100_000          # the kept rings are frozen now
        first = policy.status["bounded_last_s"]
        assert policy.bounded_pass(idle=True) is not None                     # nothing new: the frozen rings are not walked
        assert policy.status["bounded_collected"] < 100_000 + 1_000 and policy.status["bounded_last_s"] <= max(first, 0.005)
        del keep                                                             # frozen cycles become garbage
        policy.bounded_pass(idle=True)
        assert policy.status["bounded_collected"] < 100_000 + 2_000           # ... and a bounded pass cannot take them
        policy.collect()
        assert policy.status["collected"] >= 100_000                          # the chosen full collection does
        assert policy.status["automatic_full_collections"] == 0
    finally:
        gc.callbacks.remove(policy._callback)
        gc.unfreeze()
        gc.set_threshold(*before)


def test_host_gc_bounded_pass_waits_for_an_idle_loop_but_not_forever(monkeypatch):
    import gc

    from iii_drone_powerline_slam import estimator_host

    before = gc.get_threshold()
    monkeypatch.setattr(estimator_host, "GC_PASS_INTERVAL_S", 0.05)
    policy = estimator_host.HostGc()
    try:
        policy.begin()
        assert policy.bounded_pass(idle=True) is None                         # not due yet
        import time as _time
        _time.sleep(0.06)
        assert policy.bounded_pass(idle=False) is None                        # due, but the loop is busy
        _time.sleep(0.06)
        assert policy.bounded_pass(idle=False) is not None                    # twice overdue: taken anyway
    finally:
        gc.callbacks.remove(policy._callback)
        gc.unfreeze()
        gc.set_threshold(*before)
