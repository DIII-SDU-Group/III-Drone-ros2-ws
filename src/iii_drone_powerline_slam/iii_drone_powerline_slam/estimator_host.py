"""Estimator host process of the powerline_slam node.

The node's ROS process only receives (raw, serialized) messages, forwards them here in arrival order and publishes what
comes back; the estimator never shares an interpreter (or its GIL) with the ROS executor, so message intake keeps up with
the sensor rates however long a processing step takes.  This process runs the pinned powerline_slam checkout's
``IncrementalPipeline`` -- the code path of the offline III replay -- one processing epoch per activation:

* messages are deserialized and pushed in arrival order with their per-topic sequence numbers; source-time watermarks,
  not arrival order, decide when a group is processed (``advance(max_groups)`` between intake checks);
* each processed frame's passive ``Powerline`` content and native diagnostics (``iii_r1_frames.FrameConverter``) go back to
  the node, as does a runtime record once per second;
* ``flush`` closes the input, processes every buffered event, runs the single finalization and writes the replay layout;
* ``end_epoch`` discards and counts buffered events and releases the pipeline (the next epoch starts from nothing).

Node configuration (``node`` block of the runtime configuration; every key is optional and none is an estimator input):
``pipeline`` selects ``r1`` (default: the accepted reference path, ``iii_r1_pipeline.IncrementalPipeline``) or ``rt``
(``iii_rt_pipeline.RealtimePipeline``: the same estimator with the real-time camera workers and bookkeeping);
``fusion`` selects ``exact`` (default: one estimator update per distinct camera / Radar-U source stamp, the accepted
path) or ``fusion30`` (``iii_f30_pipeline``: FUSION30_v1, one update per closed 30 Hz source-time bin with the bin's
measurements transported to the fusion time; needs ``pipeline: rt``);
``evidence`` (default true) keeps the evaluation records of the final report; ``affinity`` pins the processes;
``overload`` enables REALTIME_OVERLOAD_v1 (``realtime.OverloadMonitor``).  Without ``overload`` the host is lossless
and unbounded, which is what the evaluation and parity replays use.

Protocol (pickled tuples over two one-way pipes).  Node -> host: ``("start_epoch", number, run_name)``,
``("msgs", [(key, raw, index, received_monotonic), ...])``, ``("flush",)``, ``("end_epoch",)``, ``("exit",)``.
Host -> node: ``("ready", error)``, ``("epoch_started", error)``, ``("frame", t, powerline, diagnostics)``,
``("state", value)``, ``("runtime", record)``, ``("flushed", success, summary)``, ``("epoch_ended", counts)``,
``("log", level, text)``, ``("overload", record)``, and with ``rollover`` configured (TRAVERSAL_EPOCH_v1, see ``realtime``)
``("rollover_begin", event)`` when the ``traversal_complete`` command event is met in arrival order, followed by
``("traversal_flushed", success, summary)`` once that traversal is finalized.  With ``overload`` configured a frame carries the node-clock receipt
time of its triggering message as a fifth element, so the node can bound the age of what it publishes.

With ``rollover.contract`` FAST_TRAVERSAL_EPOCH_v2 (see ``realtime``) the host keeps one warm runtime
(``iii_f30_warm.WarmRuntime``) and runs traversal generations on it.  At the boundary event it sends
``("rollover_begin", event)``, builds the next generation and sends ``("epoch_ready", generation, info)``; both
generations are stepped until the old one is sealed; a forked finalizer finishes it and the host then sends
``("traversal_final", generation, success, summary)``.  The node keeps forwarding throughout.
"""
from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

from . import realtime

ADVANCE_CHUNK = 8                 # groups between intake checks (scheduling only; IncrementalPipeline.advance(max_groups))
STATUS_PERIOD_S = 1.0
LATENCY_WINDOW = 2000
WORKER_START_TIMEOUT_S = 240.0    # worker processes: runtime activation, model load, GPU set-up
ARRIVAL_WINDOW_S = 2.0            # arrival-burst statistic: most messages received within any window of this length


def _percentiles(values) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p95": None, "p99": None, "max": None, "frames": 0}
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)  # noqa: E731
    return {"p50": pick(0.5), "p95": pick(0.95), "p99": pick(0.99), "max": round(ordered[-1], 1), "frames": len(ordered)}


class Epoch:
    """One processing epoch: a fresh pipeline from activation until deactivation (or the end of the host)."""

    def __init__(self, host, number: int, run_name: str) -> None:
        self.host = host
        rt, pl, fr, src = host.modules
        output = host.node_config.get("output_dir")
        self.number = number
        self.run_dir = None if not output else Path(output).expanduser() / run_name
        streams = (self.run_dir / "streams") if self.run_dir else Path(f"/tmp/powerline_slam_streams_{os.getpid()}_{run_name}")
        node = host.node_config
        self.realtime = host.rt_pipeline is not None
        self.workers_info = None
        # A worker process inherits the CPUs of the thread that starts it.  With a host pinned to a core of its own the
        # workers are therefore started from the workers' CPUs, never from the host's: they do not run on the host's
        # core even while they load.  The host returns to its own CPUs once every worker is pinned by role.
        plan = node.get("affinity") or {}
        # (a generation on an existing warm runtime starts no process: the host stays on its core)
        started_from = realtime.worker_cpus(plan) if plan.get("host") and host.warm is None else []
        if started_from:
            realtime.pin_process(os.getpid(), started_from)
        try:
            self._build(host, node, streams)
        finally:
            if started_from:
                realtime.pin_process(os.getpid(), plan["host"])
        if self.affinity is not None and started_from:
            self.affinity["workers_started_from"] = started_from
        self.overload = realtime.OverloadMonitor(node["overload"]) if node.get("overload") else None
        self.rollover = node.get("rollover")       # TRAVERSAL_EPOCH_v1: this epoch ends with its traversal
        self.first_source_ns: int | None = None    # source time of the epoch's first runtime input
        self.traversal: dict | None = None         # the traversal_complete event that closed this epoch
        converter = host.rt_pipeline.realtime_frame_converter(fr.FrameConverter) if self.realtime else fr.FrameConverter
        self.converter = converter(self.pipe)
        self.pipe.core.on_frame = self.on_frame
        self.ledger = [] if host.node_config.get("ledger", False) else None
        self.counters: Counter = Counter()
        self.receipt: dict[int, float] = {}
        self.latency: deque = deque(maxlen=LATENCY_WINDOW)
        self.latency_epoch: list[float] = []
        self.latency_armed: list[float] = []       # frames since the overload contract armed (after start-up)
        self.arrivals: deque = deque()             # receipt times within the last ARRIVAL_WINDOW_S
        self.arrivals_max = 0
        self.last_advance: float | None = None     # when the processed watermark last moved / the last frame was made
        self.last_frame: float | None = None
        self.last_through = -1
        self.backlog_high_water = 0
        self.arrival: dict[str, float] = {}        # last arrival (node clock) per runtime key
        self.arrival_now = None                    # newest arrival among ingested messages: the node's arrival clock
        self.absent: set[str] = set()
        self.fail_closed_reason: str | None = None
        self.finalized = False
        self.final_result: tuple[bool, dict] | None = None
        self.state = "Waiting"
        self.more = False
        self.last_status = 0.0
        # FAST_TRAVERSAL_EPOCH_v2: set once this generation met its boundary event and a successor exists
        self.boundary_ns: int | None = None
        self.boundary_since: float | None = None
        self.boundary_event_received: float | None = None
        self.passed: set[str] = set()              # runtime streams that have delivered past the boundary
        self.index_base: dict[str, int] = {}       # the node's per-topic sequence number of this generation's first message
        self.event_received: float | None = None

    def _build(self, host, node: dict, streams: Path) -> None:
        """The epoch's pipeline with its worker processes, every worker ready and pinned by role."""
        _, pl, _, src = host.modules
        self.affinity = None
        if self.realtime:
            rtp = host.rt_pipeline
            # live mission prior: the same pipeline, its nominal prior built from the exercise's command events
            pipeline_cls = rtp.RealtimePipeline if host.ol_pipeline is None else host.ol_pipeline.OnlinePipeline
            if host.f30_pipeline is not None:               # FUSION30_v1: the same pipeline with the 30 Hz tick scheduler
                pipeline_cls = (host.f30_pipeline.Fusion30Realtime if host.ol_pipeline is None
                                else host.f30_pipeline.Fusion30Online)
            timeout = float(node.get("worker_start_timeout_s", WORKER_START_TIMEOUT_S))
            if host.fast_rollover:
                # FAST_TRAVERSAL_EPOCH_v2: the worker processes and the static inputs belong to the host's warm runtime,
                # started once; a generation is a fresh pipeline on them
                if host.warm is None:
                    host.warm = host.warm_module.WarmRuntime(pl.PipelineConfig.load(host.config_path),
                                                             rtp.CameraOptions.from_node_config(node),
                                                             int(node.get("doppler_workers", 0)))
                    host.warm.wait_ready(timeout)
                self.pipe = host.warm.pipeline(pipeline_cls, clock=src.IIIClockContract(), streams_dir=streams,
                                               evidence=bool(node.get("evidence", True)))
                self.workers_info = dict(host.warm.workers.info or {}, warm_runtime=True, generation=self.pipe.f30_generation,
                                         construction_s=round(host.warm.construction_s[-1], 3))
            else:
                self.pipe = pipeline_cls(pl.PipelineConfig.load(host.config_path), clock=src.IIIClockContract(),
                                         streams_dir=streams, camera=rtp.CameraOptions.from_node_config(node),
                                         doppler_workers=int(node.get("doppler_workers", 0)),
                                         evidence=bool(node.get("evidence", True)))
                # activation completes only when every worker is ready: no model load or GPU set-up on the first frame
                self.workers_info = self.pipe.wait_ready(timeout)
        else:
            self.pipe = pl.IncrementalPipeline(pl.PipelineConfig.load(host.config_path), clock=src.IIIClockContract(),
                                               streams_dir=streams,
                                               prefetch_workers=int(node.get("prefetch_workers", 0)),
                                               doppler_workers=int(node.get("doppler_workers", 0)))
        if host.f30_pipeline is not None:
            self.pipe.f30_keep_records = bool(node.get("evidence", True))    # transport records are evaluation records
        if host.ol_pipeline is not None and self.pipe.live_priors is None:
            raise ValueError("node.mission_prior is 'live' but mission_sidecar is not a live mission prior contract")
        self.affinity = realtime.pin_workers(self.pipe, node.get("affinity") or {})

    # ------------------------------------------------------------------ intake
    def ingest(self, key: str, raw: bytes, index: int, received: float, message=None) -> bool:
        """Take one forwarded message.  True: it was the traversal_complete event that closes this epoch.

        ``message`` is the deserialized message when the host already decoded it to route it across a boundary."""
        _, _, _, src = self.host.modules
        index -= self.index_base.setdefault(key, index)        # stream indices count from zero in every generation
        self.counters[f"received_{key}"] += 1
        arrivals = self.arrivals
        arrivals.append(received)
        while arrivals[0] < received - ARRIVAL_WINDOW_S:
            arrivals.popleft()
        if len(arrivals) > self.arrivals_max:
            self.arrivals_max = len(arrivals)
        if self.finalized:
            self.counters[f"ignored_after_finalize_{key}"] += 1
            return False
        if self.fail_closed_reason is not None:
            self.counters[f"ignored_after_fail_closed_{key}"] += 1
            return False
        try:
            if message is None:
                message = self.host.deserialize(raw, self.host.types[key])
            if key == "timesync":
                self.fail("UXRCE_DDS_TIMESYNC_ACTIVE: timesync_status must stay silent (UXRCE_DDS_SYNCT=0)")
            elif key == "camera_info":
                self.pipe.verify_camera_info(message)
            elif key == "command":                          # the exercise's command event: prior bookkeeping only
                document = json.loads(message.data)
                if document.get("kind") == realtime.ROLLOVER_EVENT:
                    self.event_received = received
                    return self.traversal_complete(document)
                row = self.pipe.command(document)
                self.counters["command_refused" if "refused" in row else "command_applied"] += 1
            else:
                if key in ("camera", "radar_u"):
                    self.receipt[src.header_ns(message)] = received
                self.arrival[key] = received
                self.arrival_now = received if self.arrival_now is None else max(self.arrival_now, received)
                if self.overload is not None or self.first_source_ns is None:
                    source_time = src.source_time_ns(key, message, self.pipe.clock)
                    if self.first_source_ns is None:
                        self.first_source_ns = int(source_time)
                    if self.overload is not None:
                        self.overload.received(key, source_time, received)
                if self.realtime and key in ("camera", "radar_u"):
                    self.pipe.push(key, message, index, raw)        # the workers take the serialized message
                else:
                    self.pipe.push(key, message, index)
        except Exception as exc:
            self.counters[f"refused_{key}"] += 1
            self.fail(realtime.intake_failure_reason(key, exc))
        return False

    def traversal_complete(self, document: dict) -> bool:
        """TRAVERSAL_EPOCH_v1: accept the boundary event of this epoch's own traversal (see ``realtime``)."""
        if self.rollover is None:
            self.counters["traversal_complete_without_rollover_contract"] += 1
            return False
        source_time = int(document.get("source_time_ns", -1))
        if self.first_source_ns is None or source_time < self.first_source_ns:
            self.counters["traversal_complete_stale"] += 1   # a latched event of an earlier traversal
            return False
        self.traversal = document
        self.counters["traversal_complete"] += 1
        return True

    def fail(self, reason: str) -> None:
        if self.fail_closed_reason is None:
            self.fail_closed_reason = reason
            self.host.send(("log", "error", f"powerline_slam failed closed: {reason}"))
        self.set_state("FailClosed")

    def check_overload(self) -> None:
        """REALTIME_OVERLOAD_v1: fail closed when an input-age or queue bound is exceeded (see ``realtime``)."""
        monitor = self.overload
        if monitor is None or self.finalized or self.fail_closed_reason is not None:
            return
        monitor.processed(self.pipe.processed_through)
        head = monitor.head()
        ready = head is not None and self.pipe.core.built and self.pipe._ready(head[0])
        unprocessed = len(self.host.pending) + sum(len(queue) for queue in self.pipe.main.values())
        code = monitor.check(time.monotonic(), head_ready=ready, unprocessed_events=unprocessed)
        if code is not None:
            self.overloaded(code)

    def overloaded(self, code: str) -> None:
        """Latch the overload: nothing is processed or published as valid any more in this epoch."""
        record = self.overload.record(code)
        self.fail(record["fail_closed_reason"])
        self.host.send(("overload", record))

    # ------------------------------------------------------------------ processing
    def step(self) -> None:
        self.more = False
        if self.fail_closed_reason is None and not self.finalized:
            try:
                self.more = self.pipe.advance(max_groups=ADVANCE_CHUNK) == ADVANCE_CHUNK
            except realtime.Overloaded:
                pass                                       # latched in on_frame: nothing more is processed
            except Exception as exc:
                self.fail(f"PIPELINE_EXCEPTION: {type(exc).__name__}: {exc}")
            self.backlog_high_water = max(self.backlog_high_water, sum(self.pipe.backlog().values()))
            if self.pipe.processed_through != self.last_through:
                self.last_through = self.pipe.processed_through
                self.last_advance = time.monotonic()
            if self.boundary_ns is None:
                self.check_overload()
        now = time.monotonic()
        if self.boundary_ns is None and now - self.last_status >= STATUS_PERIOD_S:
            self.last_status = now
            self.update_stream_absence()
            self.host.send(("runtime", self.runtime()))

    def update_stream_absence(self) -> None:
        """Contract stream_absence (III_POWERLINE_SLAM_CONTRACTS_v1 with amendment A4), on the node's arrival clock.

        A runtime stream that has delivered in this epoch becomes ABSENT when its last arrival lies stall_timeout_s or more
        before the newest arrival of any ingested message *and* another stream's newest source time is more than
        stall_timeout_s past its own; it gates again as soon as it delivers.  A stream that has not delivered yet is never
        ABSENT.  Measuring silence on the arrival clock of the ingested input (not on the time this process happens to
        evaluate it) keeps processing lag from masquerading as silence."""
        if self.finalized or self.arrival_now is None:
            return
        _, pl, _, _ = self.host.modules
        timeout = float(self.host.node_config.get("stall_timeout_s", 2.0))
        last = dict(self.pipe.last)
        for key in pl.RUNTIME_KEYS:
            seen = self.arrival.get(key)
            if seen is None or last.get(key) is None:
                continue
            silent = self.arrival_now - seen >= timeout
            others = [v for k, v in last.items() if k != key and v is not None]
            advanced = bool(others) and max(others) - last[key] > timeout * 1e9
            if silent and advanced and key not in self.absent:
                self.absent.add(key)
                self.pipe.absent.add(key)
                self.counters[f"stream_absent_{key}"] += 1
                self.host.send(("log", "warning", f"powerline_slam: {key} silent for {timeout:.1f} s while other "
                                                  f"streams advanced; it no longer gates processing"))
            elif not silent and key in self.absent:
                self.absent.discard(key)
                self.pipe.absent.discard(key)
                self.counters[f"stream_rejoined_{key}"] += 1

    def on_frame(self, result, step, record) -> None:
        frame = self.converter.frame_record(result, step, record)
        t = frame["t"]
        self.last_frame = time.monotonic()
        received = self.receipt.pop(t, None)
        if received is not None:
            latency = (time.monotonic() - received) * 1000.0
            self.latency.append(latency)
            self.latency_epoch.append(latency)
        if self.realtime:
            # receipts are kept in arrival order; one whose group produced no frame is dropped once it leads
            while self.receipt and next(iter(self.receipt)) < t:
                del self.receipt[next(iter(self.receipt))]
        else:
            for stale in [k for k in self.receipt if k < t]:
                self.receipt.pop(stale, None)
        if self.overload is not None and not self.finalized:
            # a frame older than the live-age budget is never published as valid output
            code = self.overload.frame(None if received is None else time.monotonic() - received,
                                       has_lines=bool(frame["powerline"]["lines"]))
            if code is not None:
                self.overloaded(code)
                raise realtime.Overloaded(code)
            if self.overload.armed and received is not None:
                self.latency_armed.append(latency)
        if self.ledger is not None:
            self.ledger.append(frame)
        if self.overload is not None and not self.finalized:
            self.host.send(("frame", t, frame["powerline"], frame["diagnostics"], received))
        else:                                               # no age bound: unbounded mode, or a frame finished by the flush
            self.host.send(("frame", t, frame["powerline"], frame["diagnostics"]))
        self.counters["frames"] += 1
        reason = frame["diagnostics"]["fail_closed_reason"]
        self.set_state("Running" if reason in (None, "NO_CONFIRMED_CONDUCTOR") else
                       "Waiting" if reason == "V13_UNINITIALIZED" else "FailClosed")

    def set_state(self, value: str) -> None:
        if value != self.state:
            self.state = value
            if self.boundary_ns is None:               # a closing generation no longer speaks for the node's state
                self.host.send(("state", value))

    def drain(self) -> dict | None:
        """How long processing continued after the newest input arrived (meaningful once the input has stopped)."""
        if self.arrival_now is None:
            return None
        after = lambda moment: None if moment is None else round(moment - self.arrival_now, 3)  # noqa: E731
        return {"since_last_arrival_s": round(time.monotonic() - self.arrival_now, 3),
                "last_advance_after_last_arrival_s": after(self.last_advance),
                "last_frame_after_last_arrival_s": after(self.last_frame)}

    def _prior_status(self) -> dict:
        summary = self.pipe.prior_summary()
        return {"commands": summary["commands"], "causality": summary["causality"], "priors": len(summary["priors"]),
                "latest": summary["priors"][-1] if summary["priors"] else None,
                "refused": [row for row in summary["log"] if "refused" in row][-5:]}

    def runtime(self) -> dict:
        pipe = self.pipe
        return {"kind": "runtime", "epoch": self.number, "state": self.state, "fail_closed_reason": self.fail_closed_reason,
                "counters": dict(self.counters), "inbox": len(self.host.pending), "inbox_high_water": self.host.pending_high_water,
                "backlog": pipe.backlog(), "backlog_high_water": self.backlog_high_water,
                "latency_ms": _percentiles(self.latency), "latency_ms_epoch": _percentiles(self.latency_epoch),
                "latency_ms_armed": _percentiles(self.latency_armed) if self.overload is not None else None,
                "arrivals_max_per_window": {"window_s": ARRIVAL_WINDOW_S, "messages": self.arrivals_max},
                "drain": self.drain(),
                "processed_through_ns": pipe.processed_through,
                "accounting": {"received": dict(pipe.received), "rejected": dict(pipe.rejected)},
                "prefetch": pipe.prefetch_summary(),
                "live_mission_prior": None if self.host.ol_pipeline is None else self._prior_status(),
                "pipeline": "rt" if self.realtime else "r1", "affinity": self.affinity, "pid": os.getpid(),
                "warm_runtime": None if self.host.warm is None else self.host.warm_status(),
                "fusion": "exact" if self.host.f30_pipeline is None else "fusion30",
                "fusion30": None if self.host.f30_pipeline is None else pipe.fusion_status(),
                "traversal": None if self.rollover is None else {"contract": self.rollover["contract"], "first_source_ns": self.first_source_ns,
                                                               "complete": self.traversal},
                "overload": None if self.overload is None else self.overload.status()}

    # ------------------------------------------------------------------ flush and end
    def finish(self) -> tuple[bool, dict]:
        if self.final_result is not None:
            return self.final_result
        if self.overload is not None and self.overload.code is not None:      # a dead epoch is never finalized
            return False, {"error": self.fail_closed_reason, "overload": self.overload.record(self.overload.code)}
        _, pl, _, _ = self.host.modules
        self.finalized = True
        try:
            self.pipe.close_input()
            report = self.pipe.finalize(None)
            report["iii"]["mode"] = "lifecycle_node"
            report["iii"]["node_runtime"] = self.runtime()
            summary = {"status": report["status"], "frames": report["measured_frame_count"],
                       "failures": report["contract_failures"],
                       "reconstruction": (report.get("final_smoothed_reconstruction") or {}).get("status")}
            if self.rollover is not None and self.rollover.get("record_dir"):
                summary["product"] = self._write_product(report)
            if self.run_dir is not None:
                sections, sidecars = pl.layer_report(self.pipe.L, streams_info=report["iii"]["streams_summary"])
                if self.ledger is not None:
                    sidecars["jsonl"]["frame_ledger.jsonl"] = self.ledger
                pl.write_outputs(self.run_dir / "replay", report, sidecars["jsonl"], sidecars["gz"], sidecars["json_gz"])
                summary["output"] = str(self.run_dir / "replay")
                summary["streams"] = str(self.run_dir / "streams")
            self.final_result = (report["status"] == "DEVELOPMENT_REPLAY_MEASURED", summary)
        except Exception as exc:
            self.final_result = (False, {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]})
        self.host.send(("runtime", self.runtime()))
        return self.final_result

    def _write_product(self, report: dict) -> dict:
        """The traversal's final engineering product: the post-traversal reconstruction, one file per generation."""
        directory = Path(self.rollover["record_dir"]).expanduser() / f"generation_{self.number:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        reconstruction = report.get("final_smoothed_reconstruction") or {}
        document = {"schema": "iii.powerline-slam-traversal-product/v1", "generation": self.number, "traversal": self.traversal,
                    "status": report["status"], "measured_frame_count": report["measured_frame_count"],
                    "contract_failures": report["contract_failures"], "first_source_ns": self.first_source_ns,
                    "processed_through_ns": self.pipe.processed_through, "final_smoothed_reconstruction": reconstruction}
        data = json.dumps(document, default=str).encode()
        path = directory / "traversal_product.json"
        path.write_bytes(data)
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "reconstruction_status": reconstruction.get("status"), "conductors": len(reconstruction.get("conductors") or [])}

    def end(self) -> dict:
        counts = Counter({"discarded_inbox_messages_at_epoch_end": len(self.host.pending)})
        counts.update({name: value for name, value in self.counters.items() if name.startswith("ignored_after_")})
        for key, count in self.pipe.backlog().items():
            if count:
                counts[f"discarded_buffered_{key}_at_epoch_end"] += count
        self.pipe.release()
        return dict(counts)


class Host:
    def __init__(self, conn_in, conn_out, config_path: str, tools: str) -> None:
        self.conn_in, self.conn_out = conn_in, conn_out
        self.config_path = Path(config_path)
        self.tools = tools
        self.pending: deque = deque()
        self.pending_high_water = 0
        self.epoch: Epoch | None = None
        self.rt_pipeline = None
        self.ol_pipeline = None
        self.f30_pipeline = None
        self.fast_rollover = False                 # FAST_TRAVERSAL_EPOCH_v2
        self.warm_module = None
        self.warm = None                           # the warm runtime (worker pools, static inputs)
        self.closing: Epoch | None = None          # the old generation between its boundary event and its seal
        self.finalizers: list[dict] = []           # forked finalizer processes of sealed generations
        self.rollovers = 0

    def send(self, item) -> None:
        self.conn_out.send(item)

    def setup(self) -> None:
        if self.tools not in sys.path:
            sys.path.insert(0, self.tools)
        early = dict(json.loads(self.config_path.read_text()).get("node") or {})
        os.environ.update({str(k): str(v) for k, v in (early.get("environment") or {}).items()})
        import iii_r1_runtime as rt
        rt.activate()
        import iii_r1_frames as fr
        import iii_r1_pipeline as pl
        import iii_r1_source as src
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
        self.modules = (rt, pl, fr, src)
        self.deserialize = deserialize_message
        self.node_config = dict(json.loads(self.config_path.read_text()).get("node") or {})
        realtime.validate_node_config(self.node_config)
        realtime.pin_process(os.getpid(), (self.node_config.get("affinity") or {}).get("host"))
        if self.node_config.get("pipeline", "r1") == "rt":
            import iii_rt_pipeline
            self.rt_pipeline = iii_rt_pipeline
            if self.node_config.get("mission_prior", "sidecar") == "live":
                import iii_ol_pipeline
                self.ol_pipeline = iii_ol_pipeline
            if self.node_config.get("fusion", "exact") == "fusion30":
                import iii_f30_pipeline
                self.f30_pipeline = iii_f30_pipeline
            if (self.node_config.get("rollover") or {}).get("contract") == realtime.FAST_ROLLOVER_CONTRACT:
                import iii_f30_warm
                self.warm_module = iii_f30_warm
                self.fast_rollover = True
        pl.PipelineConfig.load(self.config_path)              # validates every path before any data flows
        self.types = {key: get_message(src.TYPES[key]) for key in src.TOPICS}
        self.types["camera_info"] = get_message("sensor_msgs/msg/CameraInfo")
        self.types["timesync"] = get_message("px4_msgs/msg/TimesyncStatus")
        self.types["command"] = get_message("iii_drone_interfaces/msg/StringStamped")

    def run(self) -> None:
        while True:
            busy = self.epoch is not None and (self.pending or self.epoch.more or self.closing is not None)
            if self.conn_in.poll(0 if busy else 0.02 if self.finalizers else 0.2):
                item = self.conn_in.recv()
                kind = item[0]
                if kind == "msgs":
                    self.pending.extend(item[1])
                    self.pending_high_water = max(self.pending_high_water, len(self.pending))
                elif kind == "start_epoch":
                    self.start_epoch(item[1], item[2])
                elif kind == "flush":
                    self.flush()
                elif kind == "end_epoch":
                    self.end_epoch()
                elif kind == "exit":
                    if self.epoch is not None:
                        self.end_epoch()
                    return
                continue                                   # drain every queued command/batch before processing
            if self.epoch is None:
                self.pending.clear()
                self.poll_finalizers()
                continue
            while self.pending:
                item = self.pending.popleft()
                if self.closing is not None:
                    target, message = self.route(item)
                    self.activate(target)
                    target.ingest(*item, message=message)
                    continue
                self.activate(self.epoch)
                if self.epoch.ingest(*item):
                    if self.fast_rollover:
                        self.begin_boundary()
                    else:
                        self.complete_traversal()
                        break
            if self.closing is not None:
                self.activate(self.closing)
                self.closing.step()
                self.maybe_seal()
            self.activate(self.epoch)
            self.epoch.step()
            if self.finalizers:
                self.poll_finalizers()

    # ------------------------------------------------------------------ FAST_TRAVERSAL_EPOCH_v2
    def activate(self, epoch: "Epoch") -> None:
        """The r22-r26 layers dispatch to one pipeline per process: point them at the generation about to run."""
        if self.warm is not None:
            self.warm.make_current(epoch.pipe)

    def warm_status(self) -> dict:
        return {"generations_built": self.warm.generations, "construction_s": [round(v, 3) for v in self.warm.construction_s[-5:]],
                "worker_pids": self.warm.worker_pids(), "closing_generation": None if self.closing is None else self.closing.number,
                "finalizers_running": [f["generation"] for f in self.finalizers], "rollovers": self.rollovers}

    def begin_boundary(self) -> None:
        """The current generation met its traversal_complete event: fix the routing boundary and start the successor."""
        old = self.epoch
        event = dict(old.traversal or {})
        began = time.monotonic()
        newest = max((v for v in old.pipe.last.values() if v is not None), default=-1)
        boundary = max(int(event.get("source_time_ns", -1)), int(newest))
        event["boundary_source_time_ns"] = boundary
        self.send(("rollover_begin", event))
        if old.fail_closed_reason is not None or self.closing is not None:
            # a dead generation is never finalized, and one boundary at a time: fail closed, recovery decides
            old.fail(f"{realtime.FAST_ROLLOVER_CONTRACT}:ROLLOVER_FAILED: " + (
                "the previous generation is still closing" if self.closing is not None else "the traversal's generation had failed closed"))
            return
        try:
            run = f"epoch{old.number + 1:03d}_{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
            successor = Epoch(self, old.number + 1, run)
        except Exception as exc:  # noqa: BLE001 - no successor: the node fails closed and recovery decides
            self.activate(old)
            old.fail(f"{realtime.FAST_ROLLOVER_CONTRACT}:ROLLOVER_FAILED: {type(exc).__name__}: {exc}"[:400])
            return
        old.boundary_ns, old.boundary_since, old.boundary_event_received = boundary, time.monotonic(), old.event_received
        self.closing, self.epoch = old, successor
        self.rollovers += 1
        self.send(("epoch_ready", successor.number, {
            "previous_generation": old.number, "run": run, "event": event, "boundary_source_time_ns": boundary,
            "event_received": old.event_received, "construction_s": round(time.monotonic() - began, 4),
            "host_pid": os.getpid(), "worker_pids": self.warm.worker_pids(), "rollovers": self.rollovers}))

    def route(self, item) -> tuple["Epoch", object]:
        """While a generation is closing: a runtime message goes to it when its source time is at or before the
        boundary, otherwise (and every command, camera_info and timesync message) to the new generation."""
        key, raw = item[0], item[1]
        _, pl, _, src = self.modules
        if key not in pl.RUNTIME_KEYS:
            return self.epoch, None
        try:
            message = self.deserialize(raw, self.types[key])
            source_time = src.source_time_ns(key, message, self.epoch.pipe.clock)
        except Exception:  # noqa: BLE001 - the receiving generation refuses and accounts it
            return self.epoch, None
        closing = self.closing
        if source_time <= closing.boundary_ns:
            closing.counters["routed_at_or_before_boundary"] += 1
            return closing, message
        closing.passed.add(key)
        self.epoch.counters["routed_after_boundary_while_closing"] += 1
        return self.epoch, message

    def maybe_seal(self) -> None:
        """Seal the closing generation once every stream has delivered past the boundary (or the grace time passed):
        close its input, hand the finalization to a forked process and drop it here."""
        closing = self.closing
        _, pl, _, _ = self.modules
        config = closing.rollover
        waited = time.monotonic() - closing.boundary_since
        streams = [k for k in pl.RUNTIME_KEYS if k not in closing.absent and closing.pipe.last.get(k) is not None]
        if not all(k in closing.passed for k in streams) and waited < float(config.get("seal_grace_s", realtime.FAST_ROLLOVER_DEFAULTS["seal_grace_s"])):
            return
        record = {"generation": closing.number, "boundary_source_time_ns": closing.boundary_ns, "sealed_after_boundary_event_s": round(waited, 4),
                  "streams_past_boundary": sorted(closing.passed), "streams_expected": streams,
                  "event_received": closing.boundary_event_received, "counters": dict(closing.counters)}
        self.closing = None
        if closing.fail_closed_reason is not None:
            record["error"] = f"the generation failed closed before its seal: {closing.fail_closed_reason}"
            closing.end()
            self.send(("traversal_final", closing.number, False, record))
            return
        began = time.monotonic()
        try:
            closing.pipe.close_input()                      # the synchronous seal: the generation's last ticks
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"seal: {type(exc).__name__}: {exc}"[:400]
            closing.end()
            self.send(("traversal_final", closing.number, False, record))
            return
        record["seal_s"] = round(time.monotonic() - began, 4)
        directory = Path(config["record_dir"]).expanduser() / f"generation_{closing.number:04d}" if config.get("record_dir") else Path(
            f"/tmp/powerline_slam_final_{os.getpid()}_{closing.number:04d}")
        directory.mkdir(parents=True, exist_ok=True)
        result = directory / "finalizer_result.json"
        began = time.monotonic()
        pid = os.fork()
        if pid == 0:                                        # the finalizer: an immutable copy of the sealed generation
            code = 1
            try:
                self.send = lambda item: None               # nothing of the copy reaches the node
                cpus = realtime.worker_cpus(self.node_config.get("affinity") or {})
                if cpus:                                    # off the estimator's core: the next generation is running there
                    os.sched_setaffinity(0, cpus)
                self.activate(closing)
                started = time.monotonic()
                success, summary = closing.finish()
                summary = dict(summary, finalize_s=round(time.monotonic() - started, 3))
                result.write_text(json.dumps({"success": bool(success), "summary": summary}, default=str))
                code = 0
            except BaseException as exc:  # noqa: BLE001
                try:
                    result.write_text(json.dumps({"success": False, "summary": {"error": f"{type(exc).__name__}: {exc}"[:400]}}))
                except OSError:
                    pass
            finally:
                os._exit(code)
        record["fork_s"] = round(time.monotonic() - began, 4)
        record["end_counts"] = closing.end()                # the host's own copy goes; the workers stay
        self.finalizers.append({"pid": pid, "generation": closing.number, "result": result, "forked": time.monotonic(), "record": record,
                                "deadline": time.monotonic() + float(config.get("final_timeout_s", realtime.FAST_ROLLOVER_DEFAULTS["final_timeout_s"]))})

    def poll_finalizers(self, wait: bool = False) -> None:
        for entry in list(self.finalizers):
            try:
                done, status = os.waitpid(entry["pid"], 0 if wait else os.WNOHANG)
            except ChildProcessError:
                done, status = entry["pid"], 0
            if done == 0:
                if time.monotonic() < entry["deadline"]:
                    continue
                try:
                    os.kill(entry["pid"], 9)
                    os.waitpid(entry["pid"], 0)
                except OSError:
                    pass
                self.finalizers.remove(entry)
                self.send(("traversal_final", entry["generation"], False, dict(entry["record"], error="the finalizer exceeded final_timeout_s")))
                continue
            self.finalizers.remove(entry)
            record = dict(entry["record"], final_after_seal_s=round(time.monotonic() - entry["forked"], 3), finalizer_pid=entry["pid"],
                          finalizer_exit=os.waitstatus_to_exitcode(status) if status else 0)
            try:
                outcome = json.loads(entry["result"].read_text())
            except (OSError, ValueError) as exc:
                outcome = {"success": False, "summary": {"error": f"no finalizer result: {exc}"}}
            self.send(("traversal_final", entry["generation"], bool(outcome["success"]), dict(record, final=outcome["summary"])))

    def complete_traversal(self) -> None:
        """TRAVERSAL_EPOCH_v1: every message that arrived before the event is in; finalize this traversal."""
        self.send(("rollover_begin", self.epoch.traversal))
        success, summary = self.epoch.finish()
        self.send(("traversal_flushed", success, summary))

    def start_epoch(self, number: int, run_name: str) -> None:
        if self.epoch is not None:
            self.end_epoch()
        self.pending.clear()
        self.pending_high_water = 0
        try:
            self.epoch = Epoch(self, number, run_name)
            self.send(("epoch_started", None))
        except Exception as exc:
            self.epoch = None
            self.send(("epoch_started", f"{type(exc).__name__}: {exc}"))

    def flush(self) -> None:
        if self.epoch is None:
            self.send(("flushed", False, {"error": "not active"}))
            return
        while self.pending:
            item = self.pending.popleft()
            target, message = self.route(item) if self.closing is not None else (self.epoch, None)
            self.activate(target)
            target.ingest(*item, message=message)
        if self.closing is not None:                        # the flush also seals a generation that is still closing
            self.closing.boundary_since = float("-inf")
            self.maybe_seal()
        self.activate(self.epoch)
        success, summary = self.epoch.finish()
        self.poll_finalizers(wait=True)
        self.send(("flushed", success, summary))

    def end_epoch(self) -> None:
        counts = {}
        if self.closing is not None:                        # a generation still closing ends unfinalized, and is counted
            closing, self.closing = self.closing, None
            counts["closing_generation_ended_unsealed"] = closing.number
            closing.end()
        if self.epoch is not None:
            counts.update(self.epoch.end())
            self.epoch = None
        self.pending.clear()
        self.send(("epoch_ended", counts))


def main(conn_in, conn_out, config_path: str, tools: str) -> None:
    try:
        os.setpgid(0, 0)        # a process group of its own: the worker processes share it, so the node can end them all
    except OSError:
        pass
    host = Host(conn_in, conn_out, config_path, tools)
    try:
        host.setup()
    except Exception as exc:
        conn_out.send(("ready", f"{type(exc).__name__}: {exc}"))
        return
    conn_out.send(("ready", None))
    try:
        host.run()
    except (EOFError, BrokenPipeError):
        pass                                               # the node went away: nothing may outlive it
    finally:
        if host.epoch is not None:
            host.epoch.pipe.release()
        if host.warm is not None:
            host.warm.shutdown()
