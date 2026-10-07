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

TRAVERSAL_EPOCH_v1
==================
Enabled by a ``rollover`` block (needs ``mission_prior: live``, whose command stream carries the boundary)::

    {"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": 180.0, "record_dir": "/path/or/null"}

One completed corridor traversal is one estimator epoch (generation).  The flight exercise publishes a
``traversal_complete`` command event once its last prescribed maneuver has completed; the estimator host meets it in
arrival order, after every message that arrived before it.  The transaction, in order: (1) the event is received;
(2) nothing more enters the old epoch (later arrivals are counted, per stream, as the rollover gap); (3) the traversal is
flushed and finalized; (4) its final record and product are persisted and emitted; (5) the estimator host process and
its workers are destroyed -- no estimator state can cross the boundary; (6) the generation number is incremented and a
new host with a fresh pipeline is started; (7) ``traversal_epoch`` / ``ready`` is published.  A failure or a
transaction longer than ``timeout_s`` fails the node closed (``TRAVERSAL_EPOCH_v1:ROLLOVER_FAILED`` /
``ROLLOVER_TIMEOUT``).  An event whose source time lies before the epoch's first input (a latched event of an earlier
traversal) is counted and ignored; an epoch that already failed closed is never finalized.

BOUNDED_RECOVERY_v1
===================
Enabled by a ``recovery`` block::

    {"contract": "BOUNDED_RECOVERY_v1", "max_attempts": 3, "window_s": 900.0, "cooldown_s": 5.0, "backoff": 2.0,
     "state_file": "/path/recovery_state.json"}

The fail-closed contracts above stay as they are: a trigger immediately suppresses every valid output.  This is only a
bounded outer policy for what happens next.

* Health condition for an attempt: the epoch failed closed with a *recoverable* reason (``RECOVERABLE``: the overload
  codes, a rollover failure or time-out, a pipeline exception such as a lost worker) or the node process was respawned
  after it ended while active.  Input-contract violations (e.g. PX4 time synchronization active) are not recoverable.
* Budget: at most ``max_attempts`` attempts within any ``window_s`` seconds, counted in ``state_file`` so that process
  respawns count too.
* Cool-down and back-off: attempt n starts ``cooldown_s * backoff ** (n - 1)`` seconds after the failure.
* Epoch reset: an attempt never resumes anything.  The estimator host process and its workers are destroyed, a new
  host is started and a new epoch (generation + 1) begins from nothing.
* Exhausted: when the budget is used up the node stays failed closed (``BOUNDED_RECOVERY_v1:EXHAUSTED``), publishes
  that record once per second and attempts nothing more -- also after a process respawn -- until an operator calls the
  node's ``reset_recovery`` service.  Every attempt and its outcome is published on ``diagnostics`` (kind ``recovery``).

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
ROLLOVER_CONTRACT = "TRAVERSAL_EPOCH_v1"
ROLLOVER_EVENT = "traversal_complete"
RECOVERY_CONTRACT = "BOUNDED_RECOVERY_v1"
RECOVERY_KEYS = {"max_attempts": int, "window_s": float, "cooldown_s": float, "backoff": float}
# fail-closed reasons after which a recovery attempt is permitted (prefix match)
RECOVERABLE = ("REALTIME_OVERLOAD_v1:", "TRAVERSAL_EPOCH_v1:", "PIPELINE_EXCEPTION", "PROCESS_RESPAWN", "HOST_LOST", "RECOVERY_FAILED",
               "NODE_INTERNAL:")
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
    rollover = node.get("rollover")
    if rollover is not None:
        if rollover.get("contract") != ROLLOVER_CONTRACT:
            raise ValueError(f"node.rollover.contract must be {ROLLOVER_CONTRACT}")
        timeout = rollover.get("timeout_s")
        if not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0):
            raise ValueError("node.rollover.timeout_s must be a positive number")
        if rollover.get("record_dir") is not None and not isinstance(rollover["record_dir"], str):
            raise ValueError("node.rollover.record_dir must be a path or null")
        unknown = sorted(set(rollover) - {"contract", "timeout_s", "record_dir"})
        if unknown:
            raise ValueError(f"node.rollover: unknown keys {unknown}")
        if node.get("mission_prior", "sidecar") != "live":
            raise ValueError("node.rollover needs node.mission_prior 'live' (the command stream carries the boundary)")
    recovery = node.get("recovery")
    if recovery is not None:
        if recovery.get("contract") != RECOVERY_CONTRACT:
            raise ValueError(f"node.recovery.contract must be {RECOVERY_CONTRACT}")
        for key in RECOVERY_KEYS:
            value = recovery.get(key)
            if not (isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0):
                raise ValueError(f"node.recovery.{key} must be a positive number")
        if int(recovery["max_attempts"]) != recovery["max_attempts"] or recovery["backoff"] < 1:
            raise ValueError("node.recovery.max_attempts must be a whole number and backoff at least 1")
        if not isinstance(recovery.get("state_file"), str) or not recovery["state_file"]:
            raise ValueError("node.recovery.state_file must be a path")
        unknown = sorted(set(recovery) - set(RECOVERY_KEYS) - {"contract", "state_file"})
        if unknown:
            raise ValueError(f"node.recovery: unknown keys {unknown}")
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


# --------------------------------------------------------------------------------------------------------- recovery
class RecoveryLedger:
    """BOUNDED_RECOVERY_v1 bookkeeping, kept in a file so that it outlives the process (pure logic; the node acts)."""

    def __init__(self, config: dict, clock=None) -> None:
        import json
        import time
        from pathlib import Path
        self._json, self._clock = json, clock or time.time
        self.config = dict(config)
        self.path = Path(config["state_file"]).expanduser()
        self.max_attempts, self.window = int(config["max_attempts"]), float(config["window_s"])
        self.cooldown, self.backoff = float(config["cooldown_s"]), float(config["backoff"])
        self.state = {"attempts": [], "exhausted": None, "active": False, "host_group": None, "resets": 0}
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self.state.update({key: loaded[key] for key in self.state if key in loaded})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(self._json.dumps(self.state, indent=1) + "\n")
        temporary.replace(self.path)

    @staticmethod
    def recoverable(reason: str) -> bool:
        return str(reason).startswith(RECOVERABLE)

    def recent(self) -> list[dict]:
        now = self._clock()
        return [attempt for attempt in self.state["attempts"] if now - attempt["t"] <= self.window]

    @property
    def exhausted(self) -> dict | None:
        return self.state["exhausted"]

    def next_attempt(self, reason: str) -> dict | None:
        """Register one attempt; None when the budget is used up (the exhausted state is latched and saved)."""
        if self.state["exhausted"] is not None:
            return None
        recent = self.recent()
        now = self._clock()
        if len(recent) >= self.max_attempts:
            self.state["exhausted"] = {"t": now, "reason": reason, "attempts_in_window": len(recent), "window_s": self.window}
            self.save()
            return None
        number = len(recent) + 1
        attempt = {"t": now, "reason": reason, "number": number, "delay_s": round(self.cooldown * self.backoff ** (number - 1), 3)}
        self.state["attempts"] = recent + [attempt]
        self.save()
        return attempt

    def mark_active(self, active: bool) -> None:
        if self.state["active"] != bool(active):
            self.state["active"] = bool(active)
            self.save()

    def set_host_group(self, group: dict | None) -> None:
        self.state["host_group"] = group
        self.save()

    def reset(self) -> None:
        self.state.update(attempts=[], exhausted=None, resets=int(self.state.get("resets", 0)) + 1)
        self.save()

    def status(self) -> dict:
        return {"contract": RECOVERY_CONTRACT, "max_attempts": self.max_attempts, "window_s": self.window,
                "cooldown_s": self.cooldown, "backoff": self.backoff, "attempts_in_window": len(self.recent()),
                "exhausted": self.state["exhausted"], "resets": self.state.get("resets", 0)}


def process_start_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def kill_process_group(group: int, not_before_ticks: int | None = None) -> list[int]:
    """SIGKILL what is left of an estimator host's process group (the host makes itself a group leader, so its
    worker processes share the group).  Only python multiprocessing processes of that group that started no earlier
    than ``not_before_ticks`` are touched.  Returns the process ids signalled."""
    import signal
    killed = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            if int(fields[2]) != group or pid == os.getpid():
                continue
            if not_before_ticks is not None and int(fields[19]) < not_before_ticks:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                if b"multiprocessing" not in handle.read():
                    continue
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return killed


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
