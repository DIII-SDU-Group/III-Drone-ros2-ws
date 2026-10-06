"""Real-time operation of the powerline_slam node: the overload contract, CPU pinning and configuration checks.

Nothing here touches estimator inputs, ordering or outputs.  Ages are measured on the node's monotonic receipt clock and
bound what may be *published*; source time alone still decides what the estimator processes and when.

REALTIME_OVERLOAD_v1
====================
Enabled by an ``overload`` block in the node configuration::

    {"contract": "REALTIME_OVERLOAD_v1", "live_age_budget_s": 2.0, "startup_timeout_s": 10.0,
     "max_unprocessed_events": 5000, "max_node_queue": 1000}

Without the block the node is lossless and unbounded (evaluation and parity replays).

An epoch starts in the *startup* phase and becomes *armed* at its first frame whose age -- node receipt of the frame's
triggering camera or radar message until the frame is converted for publication -- is within ``live_age_budget_s``.

Triggers (the first one latches; ``fail_closed_reason`` is ``REALTIME_OVERLOAD_v1:<CODE>``):

``OUTPUT_AGE``       a frame is older than the budget and either the epoch is armed or the frame carries conductor
                     lines.  A stale frame with valid lines is therefore never published in any phase; a stale empty
                     frame is published only during startup.
``INPUT_AGE``        armed, and the oldest unprocessed camera/radar/odometry event whose input watermarks are already
                     satisfied -- so only processing capacity or worker results hold it -- has waited longer than the
                     budget.  Events that wait for another stream's data are starved, not overloaded, and do not count.
``QUEUE_DEPTH``      more than ``max_unprocessed_events`` events are received but unprocessed in the host (any phase).
``STARTUP_TIMEOUT``  the epoch is still not armed ``startup_timeout_s`` after its first runtime input.
``NODE_QUEUE``       (node side) more than ``max_node_queue`` messages wait to be forwarded: the host is not reading.
``NODE_PUBLISH_AGE`` (node side) a frame with conductor lines is older than the budget when it is about to be published.

After a trigger the epoch is dead: the host processes nothing more and discards its buffers, later inputs are counted
and dropped, no further frame is published, and the node publishes state ``FailClosed``, an empty ``Powerline`` and an
overload diagnostics record (repeated once per second).  Nothing is resumed and no interval is reconstructed: recovery
is a deactivate/activate cycle (a fresh processing epoch) or a process restart.  ``flush_and_finalize`` -- an explicit
post-traversal evaluation step -- disarms the contract for the frames it finishes.
"""
from __future__ import annotations

from collections import deque
import os

CONTRACT = "REALTIME_OVERLOAD_v1"
MAIN_KEYS = ("camera", "radar_u", "odometry")       # the keys whose events form the estimator's source-time groups
OVERLOAD_KEYS = {"contract": str, "live_age_budget_s": float, "startup_timeout_s": float, "max_unprocessed_events": int,
                 "max_node_queue": int}
AFFINITY_ROLES = ("node", "host", "detector_workers", "mask_worker", "mask_fallback_workers", "radar_workers",
                  "doppler_workers", "prefetch_workers")
REQUIRED_ROLES = {"r1": ("node", "host", "prefetch_workers", "doppler_workers"),
                  "rt": ("node", "host", "detector_workers", "mask_worker", "mask_fallback_workers", "radar_workers",
                         "doppler_workers")}
WORKER_COUNT_DEFAULTS = {"prefetch_workers": 0, "doppler_workers": 0, "detector_workers": 3, "radar_workers": 1,
                         "mask_fallback_workers": 0}


class Overloaded(RuntimeError):
    """Raised inside the processing step when a frame trips the overload contract (the latch is already set)."""


def validate_node_config(node: dict) -> None:
    """Refuse a node configuration this version cannot honour (before any process or worker starts)."""
    if node.get("pipeline", "r1") not in ("r1", "rt"):
        raise ValueError(f"node.pipeline must be 'r1' or 'rt', not {node.get('pipeline')!r}")
    if node.get("mission_prior", "sidecar") not in ("sidecar", "live"):
        raise ValueError(f"node.mission_prior must be 'sidecar' or 'live', not {node.get('mission_prior')!r}")
    if node.get("mission_prior", "sidecar") == "live" and node.get("pipeline", "r1") != "rt":
        raise ValueError("node.mission_prior 'live' needs node.pipeline 'rt'")
    if node.get("intake", "executor") not in ("executor", "waitset"):
        raise ValueError(f"node.intake must be 'executor' or 'waitset', not {node.get('intake')!r}")
    if node.get("mask_device", "cpu") not in ("cpu", "cuda"):
        raise ValueError(f"node.mask_device must be 'cpu' or 'cuda', not {node.get('mask_device')!r}")
    affinity = node.get("affinity") or {}
    for role, cpus in affinity.items():
        if role not in AFFINITY_ROLES:
            raise ValueError(f"node.affinity: unknown role {role!r}")
        if not isinstance(cpus, list) or not cpus or not all(isinstance(c, int) and c >= 0 for c in cpus):
            raise ValueError(f"node.affinity.{role} must be a non-empty list of CPU numbers")
    if affinity:
        # a child process inherits its parent's CPUs: a partial plan would silently stack processes on one set
        missing = [role for role in REQUIRED_ROLES[node.get("pipeline", "r1")] if role not in affinity
                   and not (role in WORKER_COUNT_DEFAULTS and int(node.get(role, WORKER_COUNT_DEFAULTS[role])) <= 0)]
        if missing:
            raise ValueError(f"node.affinity must name every process role of the pipeline; missing {missing}")
    overload = node.get("overload")
    if overload is not None:
        if overload.get("contract") != CONTRACT:
            raise ValueError(f"node.overload.contract must be {CONTRACT}")
        for key, kind in OVERLOAD_KEYS.items():
            if key not in overload:
                raise ValueError(f"node.overload.{key} is required")
            if kind is not str and not (isinstance(overload[key], (int, float)) and not isinstance(overload[key], bool)
                                        and overload[key] > 0):
                raise ValueError(f"node.overload.{key} must be a positive number")
        unknown = sorted(set(overload) - set(OVERLOAD_KEYS))
        if unknown:
            raise ValueError(f"node.overload: unknown keys {unknown}")


# ---------------------------------------------------------------------------------------------------------- pinning
def pin_process(pid: int, cpus) -> list[int] | None:
    """Set the CPU affinity of every thread of a process (threads created later inherit their creator's)."""
    if not cpus:
        return None
    try:
        tids = [int(entry) for entry in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        tids = [pid]
    for tid in tids:
        try:
            os.sched_setaffinity(tid, cpus)
        except OSError:
            pass                                          # a thread that ended meanwhile
    return sorted(cpus)


def worker_cpus(affinity: dict) -> list[int]:
    """Every CPU a worker role of the plan may use: where worker processes are started (see ``estimator_host.Epoch``)."""
    cpus: set[int] = set()
    for role in AFFINITY_ROLES:
        if role not in ("node", "host"):
            cpus.update(affinity.get(role) or [])
    return sorted(cpus)


def pin_workers(pipe, affinity: dict) -> dict:
    """Pin the pipeline's worker processes by role; returns what was pinned (recorded in the runtime record)."""
    camera = getattr(pipe, "prefetcher", None)
    doppler = getattr(pipe, "doppler_solvers", None)
    pools = {"detector_workers": getattr(camera, "detectors", None), "mask_worker": getattr(camera, "masks", None),
             "mask_fallback_workers": getattr(camera, "fallback", None),
             "radar_workers": getattr(camera, "radar", None), "prefetch_workers": getattr(camera, "pool", None),
             "doppler_workers": getattr(doppler, "pool", None)}
    record = {}
    for role, pool in pools.items():
        cpus = affinity.get(role)
        if pool is None or not cpus:
            continue
        pids = sorted(getattr(pool, "_processes", None) or {})
        for pid in pids:
            pin_process(pid, cpus)
        record[role] = {"cpus": sorted(cpus), "pids": pids}
    if affinity.get("host"):
        record["host"] = {"cpus": sorted(affinity["host"]), "pids": [os.getpid()]}
    return record


# --------------------------------------------------------------------------------------------------------- overload
class OverloadMonitor:
    """Host side of REALTIME_OVERLOAD_v1 for one processing epoch (pure bookkeeping; the host acts on the codes)."""

    def __init__(self, config: dict) -> None:
        self.config = dict(config)
        self.budget = float(config["live_age_budget_s"])
        self.startup_timeout = float(config["startup_timeout_s"])
        self.max_unprocessed = int(config["max_unprocessed_events"])
        self.pending: dict[str, deque] = {key: deque() for key in MAIN_KEYS}     # (source time, receipt) in arrival order
        self.first_input: float | None = None
        self.armed = False
        self.arming_frame_age_s: float | None = None
        self.code: str | None = None
        self.frames = {"startup": 0, "startup_stale_empty": 0, "armed": 0}
        self.high = {"input_age_s": 0.0, "output_age_armed_s": 0.0, "output_age_startup_s": 0.0, "unprocessed_events": 0}
        self.trigger: dict | None = None

    # ------------------------------------------------------------------ bookkeeping
    def received(self, key: str, source_time_ns: int, receipt: float) -> None:
        if self.first_input is None:
            self.first_input = receipt
        queue = self.pending.get(key)
        if queue is not None:
            queue.append((int(source_time_ns), receipt))

    def processed(self, through_ns: int) -> None:
        """Every event at or before the processed source time is done, whether or not it produced a frame."""
        for queue in self.pending.values():
            while queue and queue[0][0] <= through_ns:
                queue.popleft()

    def head(self) -> tuple[int, float] | None:
        """(source time, receipt) of the next event the estimator will process."""
        heads = [queue[0] for queue in self.pending.values() if queue]
        return min(heads) if heads else None

    # ------------------------------------------------------------------ rules
    def check(self, now: float, *, head_ready: bool, unprocessed_events: int) -> str | None:
        if self.code is not None:
            return None
        self.high["unprocessed_events"] = max(self.high["unprocessed_events"], unprocessed_events)
        if unprocessed_events > self.max_unprocessed:
            return self._trip("QUEUE_DEPTH", unprocessed_events=unprocessed_events)
        if self.armed:
            head = self.head()
            if head_ready and head is not None:
                age = now - head[1]
                self.high["input_age_s"] = max(self.high["input_age_s"], age)
                if age > self.budget:
                    return self._trip("INPUT_AGE", input_age_s=round(age, 3), head_source_time_ns=head[0])
        elif self.first_input is not None and now - self.first_input > self.startup_timeout:
            return self._trip("STARTUP_TIMEOUT", since_first_input_s=round(now - self.first_input, 3))
        return None

    def frame(self, age_s: float | None, *, has_lines: bool) -> str | None:
        """Age rule for one frame about to be sent for publication; arms the epoch at its first in-budget frame."""
        if self.code is not None or age_s is None:
            return None
        if age_s <= self.budget:
            if not self.armed:
                self.armed = True
                self.arming_frame_age_s = round(age_s, 3)
            self.frames["armed"] += 1
            self.high["output_age_armed_s"] = max(self.high["output_age_armed_s"], age_s)
            return None
        if self.armed or has_lines:
            return self._trip("OUTPUT_AGE", output_age_s=round(age_s, 3), armed=self.armed, has_lines=has_lines)
        self.frames["startup_stale_empty"] += 1
        self.high["output_age_startup_s"] = max(self.high["output_age_startup_s"], age_s)
        return None

    def _trip(self, code: str, **detail) -> str:
        self.code = code
        self.trigger = {"code": code, **detail}
        return code

    # ------------------------------------------------------------------ records
    def status(self) -> dict:
        return {"contract": CONTRACT, "armed": self.armed, "arming_frame_age_s": self.arming_frame_age_s, "code": self.code,
                "frames": dict(self.frames),
                "high_water": {key: round(value, 3) if isinstance(value, float) else value for key, value in self.high.items()},
                "pending_events": {key: len(queue) for key, queue in self.pending.items()}}

    def record(self, code: str) -> dict:
        return {"kind": "overload", "contract": CONTRACT, "fail_closed_reason": f"{CONTRACT}:{code}", "trigger": self.trigger,
                "limits": {key: self.config[key] for key in OVERLOAD_KEYS if key != "contract"}, "status": self.status(),
                "recovery": "deactivate and activate (fresh processing epoch) or restart the process"}
