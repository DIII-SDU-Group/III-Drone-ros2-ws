#!/usr/bin/env python3
"""Record that the physical PX4 stays disarmed throughout a HIL run.

HIL flies the workstation PX4 SITL; the physical flight controller stays
connected to the Pi over Ethernet and broadcasts MAVLink heartbeats to the
Pi's HIL MAVLink port (UDP 14542). This passive listener runs on the Pi,
samples those heartbeats once per second into
`physical_px4_heartbeats.jsonl`, and writes `physical_px4_disarm.json` when it
is interrupted or its duration ends. It never sends anything to PX4.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import socket
import time
from typing import Any

HIL_MAVLINK_PORT = 14542
PX4_PEER = "10.41.10.2"
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAVLINK_MSG_ID_HEARTBEAT = 0
# A sample must arrive at least this often for the record to be continuous.
MAX_SAMPLE_GAP_SEC = 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize(samples: list[dict[str, Any]], started_at: float, finished_at: float,
              heartbeats: int, peer: str = PX4_PEER) -> dict[str, Any]:
    """Judge the sampled record: continuous coverage from the expected peer, never armed."""
    stamps = [started_at] + [sample["t"] for sample in samples] + [finished_at]
    max_gap = max((b - a for a, b in zip(stamps, stamps[1:])), default=finished_at - started_at)
    armed = [sample for sample in samples if sample["armed"]]
    peers = sorted({sample["src"] for sample in samples})
    failures = []
    if not samples:
        failures.append("no physical PX4 heartbeat received")
    if armed:
        failures.append(f"physical PX4 reported armed in {len(armed)} samples")
    if max_gap > MAX_SAMPLE_GAP_SEC:
        failures.append(f"heartbeat record has a {max_gap:.1f} s gap")
    if samples and peers != [peer]:
        failures.append(f"unexpected heartbeat sources {peers}")
    return {
        "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "finished_at": datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
        "observed_sec": round(finished_at - started_at, 1),
        "heartbeats": heartbeats,
        "samples": len(samples),
        "armed_samples": len(armed),
        "max_gap_sec": round(max_gap, 2),
        "peers": peers,
        "system_ids": sorted({sample["sysid"] for sample in samples}),
        "remained_disarmed": not failures,
        "failures": failures,
    }


def heartbeat_fields(data: bytes) -> list[dict[str, int]]:
    """Heartbeats in one UDP datagram (MAVLink v1 or v2, no signing check)."""
    found = []
    index = 0
    while index < len(data):
        magic = data[index]
        if magic == 0xFD and index + 10 <= len(data):
            length, flags = data[index + 1], data[index + 2]
            sysid, msgid = data[index + 5], int.from_bytes(data[index + 7:index + 10], "little")
            header = 10
            end = index + header + length + 2 + (13 if flags & 1 else 0)
        elif magic == 0xFE and index + 6 <= len(data):
            length = data[index + 1]
            sysid, msgid = data[index + 3], data[index + 5]
            header = 6
            end = index + header + length + 2
        else:
            index += 1
            continue
        if end > len(data):
            break
        if msgid == MAVLINK_MSG_ID_HEARTBEAT:
            # MAVLink v2 truncates trailing zero bytes; pad to the full 9-byte payload.
            payload = data[index + header:index + header + length].ljust(9, b"\0")
            found.append({"sysid": sysid,
                          "custom_mode": int.from_bytes(payload[0:4], "little"),
                          "type": payload[4], "autopilot": payload[5],
                          "base_mode": payload[6], "system_status": payload[7]})
        index = end
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--port", type=int, default=HIL_MAVLINK_PORT)
    parser.add_argument("--peer", default=PX4_PEER)
    args = parser.parse_args(argv)

    stop = {"requested": False}

    def request_stop(*_: Any) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # PX4 broadcasts to this port; SO_REUSEADDR lets another passive listener coexist.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.5)

    started = time.time()
    samples: list[dict[str, Any]] = []
    heartbeats = 0
    last_sample = 0.0
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    with open(args.artifact_dir / "physical_px4_heartbeats.jsonl", "w") as record:
        while not stop["requested"] and time.time() - started < args.duration_sec:
            try:
                data, (src, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except InterruptedError:
                continue
            now = time.time()
            for heartbeat in heartbeat_fields(data):
                heartbeats += 1
                armed = bool(heartbeat["base_mode"] & MAV_MODE_FLAG_SAFETY_ARMED)
                # Keep every armed heartbeat, otherwise one sample per second.
                if armed or now - last_sample >= 1.0:
                    last_sample = now
                    sample = {"t": now, "src": src, **heartbeat, "armed": armed}
                    samples.append(sample)
                    record.write(json.dumps(sample) + "\n")
                    record.flush()
    summary = summarize(samples, started, time.time(), heartbeats, peer=args.peer)
    (args.artifact_dir / "physical_px4_disarm.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("remained_disarmed", "samples", "armed_samples", "max_gap_sec")}))
    return 0 if summary["remained_disarmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
