"""Offline tests for the physical PX4 disarm monitor."""

from __future__ import annotations

from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import physical_px4_disarm_monitor as monitor  # noqa: E402


def v2_heartbeat(base_mode: int, sysid: int = 1, truncate: bool = True) -> bytes:
    payload = struct.pack("<IBBBBB", 251920384, 2, 12, base_mode, 0, 3)
    if truncate:
        payload = payload.rstrip(b"\0")
    header = bytes([0xFD, len(payload), 0, 0, 7, sysid, 1]) + (0).to_bytes(3, "little")
    return header + payload + b"\x00\x00"


def v1_heartbeat(base_mode: int) -> bytes:
    payload = struct.pack("<IBBBBB", 0, 2, 12, base_mode, 3, 3)
    return bytes([0xFE, len(payload), 5, 1, 1, 0]) + payload + b"\x00\x00"


def test_parses_v2_and_v1_heartbeats_and_armed_flag() -> None:
    other = bytes([0xFD, 2, 0, 0, 8, 1, 1]) + (30).to_bytes(3, "little") + b"\x01\x02\x00\x00"
    found = monitor.heartbeat_fields(v2_heartbeat(17) + other + v1_heartbeat(17 | 128))
    assert [h["base_mode"] for h in found] == [17, 145]
    assert found[0]["custom_mode"] == 251920384 and found[0]["sysid"] == 1


def test_truncated_or_garbage_datagrams_yield_nothing() -> None:
    assert monitor.heartbeat_fields(b"\x00\x01garbage") == []
    assert monitor.heartbeat_fields(v2_heartbeat(17)[:8]) == []


def sample(t: float, armed: bool = False, src: str = "10.41.10.2") -> dict:
    return {"t": t, "src": src, "sysid": 1, "armed": armed}


def test_continuous_disarmed_record_is_accepted() -> None:
    summary = monitor.summarize([sample(100.5 + i) for i in range(10)], 100.0, 110.0, heartbeats=20)
    assert summary["remained_disarmed"], summary["failures"]
    assert summary["max_gap_sec"] == 1.0


def test_armed_gap_foreign_peer_and_silence_are_rejected() -> None:
    assert "physical PX4 reported armed in 1 samples" in monitor.summarize(
        [sample(101), sample(102, armed=True)], 100.0, 103.0, heartbeats=4)["failures"]
    assert any("gap" in f for f in monitor.summarize(
        [sample(101), sample(110)], 100.0, 111.0, heartbeats=2)["failures"])
    assert any("gap" in f for f in monitor.summarize(
        [sample(101)], 100.0, 120.0, heartbeats=2)["failures"])
    assert any("unexpected heartbeat sources" in f for f in monitor.summarize(
        [sample(101, src="10.41.10.9")], 100.0, 102.0, heartbeats=1)["failures"])
    silent = monitor.summarize([], 100.0, 102.0, heartbeats=0)
    assert "no physical PX4 heartbeat received" in silent["failures"]
    assert not silent["remained_disarmed"]
