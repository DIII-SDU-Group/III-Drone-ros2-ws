#!/usr/bin/env python3
"""
ros2_bag_export.py

Export selected topics from ROS 2 bag(s) to per-topic CSV with fully flattened fields.

Usage:
  python ros2_bag_export.py --out ./export \
    --include "^/my_topic$" --include "^/other/.*" \
    --exclude "^/camera/image_raw$" \
    /path/to/bag1 /path/to/bag2
"""

import argparse
import csv
import json
import os
import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

# ROS 2 imports (must run in a ROS 2 Python env)
from rosidl_runtime_py.utilities import get_message
from rosidl_runtime_py import message_to_ordereddict
from rclpy.serialization import deserialize_message
from rosbag2_py import SequentialReader, StorageOptions, ConverterOptions


# ---------- helpers ----------

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "bag"


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def ns_to_s(ns: int) -> float:
    return ns / 1e9


def has_header_stamp(flat: Dict[str, Any]) -> bool:
    return ("header.stamp.sec" in flat) and ("header.stamp.nanosec" in flat)


def flatten_obj(obj: Any, prefix: str = "") -> OrderedDict:
    """
    Flatten a nested structure (from message_to_ordereddict) into dot/bracket keys:
      - dict keys -> dot notation (a.b.c)
      - lists/tuples -> bracket indices (arr[0].x)
      - scalars -> direct assignment
      - also emits <prefix>.__len__ for lists
    """
    out = OrderedDict()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.update(flatten_obj(v, p))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            out.update(flatten_obj(v, p))
        out[f"{prefix}.__len__"] = len(obj)
    else:
        out[prefix] = obj
    return out


# ---------- bag reading ----------

def open_reader(bag_path: str) -> Tuple[SequentialReader, Dict[str, str]]:
    storage_options = StorageOptions(uri=bag_path, storage_id="sqlite3")
    converter_options = ConverterOptions(input_serialization_format="cdr",
                                         output_serialization_format="cdr")
    reader = SequentialReader()
    reader.open(storage_options, converter_options)
    topics_to_types = {md.name: md.type for md in reader.get_all_topics_and_types()}
    return reader, topics_to_types


def iter_messages(bag_path: str) -> Iterable[Tuple[str, Any, int]]:
    reader, topics_to_types = open_reader(bag_path)
    while reader.has_next():
        topic, raw, t_ns = reader.read_next()
        type_str = topics_to_types.get(topic)
        if not type_str:
            continue
        try:
            msg_type = get_message(type_str)
            msg = deserialize_message(raw, msg_type)
        except Exception as e:
            print(f"[WARN] Failed to deserialize {topic} @ {t_ns}: {e}")
            continue
        yield topic, msg, t_ns


# ---------- passes ----------

def first_pass_metadata(
    bag_path: str,
    exclude_res: List[re.Pattern],
    include_res: List[re.Pattern],
) -> Tuple[Dict[str, Dict], int, int, int]:
    """
    Per-topic counts + global start/end.
    Returns (per_topic, total_count, first_t, last_t) where per_topic[topic] has
    {'type','count','first_t','last_t'} for included topics.
    """
    reader, topics_to_types = open_reader(bag_path)
    per_topic: Dict[str, Dict] = {}
    total, first_t, last_t = 0, None, None

    while reader.has_next():
        topic, _, t = reader.read_next()
        if any(r.search(topic) for r in exclude_res):
            continue
        if include_res and not any(r.search(topic) for r in include_res):
            continue

        if topic not in per_topic:
            per_topic[topic] = {
                "type": topics_to_types.get(topic, "unknown"),
                "count": 0,
                "first_t": t,
                "last_t": t,
            }
        md = per_topic[topic]
        md["count"] += 1
        md["last_t"] = t
        if first_t is None or t < first_t:
            first_t = t
        if last_t is None or t > last_t:
            last_t = t
        total += 1

    return per_topic, total, first_t or 0, last_t or 0


def collect_topic_header_keys(
    bag_path: str, target_topic: str
) -> List[str]:
    """
    Second pass over one topic to build a union header of flattened keys.
    Ensures timestamp columns first: stamp_sec, stamp_nsec, stamp
    """
    keys = OrderedDict()
    for topic, msg, t_ns in iter_messages(bag_path):
        if topic != target_topic:
            continue
        flat = flatten_obj(message_to_ordereddict(msg))

        # Prefer embedded header.stamp if available
        if has_header_stamp(flat):
            sec = flat.get("header.stamp.sec", 0)
            nsec = flat.get("header.stamp.nanosec", 0)
            flat["stamp_sec"] = int(sec)
            flat["stamp_nsec"] = int(nsec)
            flat["stamp"] = float(sec) + float(nsec) * 1e-9
        else:
            flat["stamp_sec"] = int(t_ns // 1_000_000_000)
            flat["stamp_nsec"] = int(t_ns % 1_000_000_000)
            flat["stamp"] = ns_to_s(t_ns)

        for k in flat.keys():
            keys.setdefault(k, None)

    cols = list(keys.keys())
    for ts in ["stamp_sec", "stamp_nsec", "stamp"]:
        if ts in cols:
            cols.remove(ts)
    return ["stamp_sec", "stamp_nsec", "stamp"] + cols


def write_topic_csv(
    bag_path: str,
    topic: str,
    type_str: str,
    out_csv_path: Path,
    header: List[str],
) -> int:
    """
    Third pass over one topic to write CSV with given header (rectangular).
    Returns rows written.
    """
    count = 0
    with out_csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()

        for t_topic, msg, t_ns in iter_messages(bag_path):
            if t_topic != topic:
                continue

            flat = flatten_obj(message_to_ordereddict(msg))

            if has_header_stamp(flat):
                sec = int(flat.get("header.stamp.sec", 0))
                nsec = int(flat.get("header.stamp.nanosec", 0))
                flat["stamp_sec"] = sec
                flat["stamp_nsec"] = nsec
                flat["stamp"] = float(sec) + float(nsec) * 1e-9
            else:
                flat["stamp_sec"] = int(t_ns // 1_000_000_000)
                flat["stamp_nsec"] = int(t_ns % 1_000_000_000)
                flat["stamp"] = ns_to_s(t_ns)

            row = {k: flat.get(k, None) for k in header}
            writer.writerow(row)
            count += 1

    return count


# ---------- export orchestrator ----------

def export_bag(
    bag_path: str,
    out_root: Path,
    exclude_res: List[re.Pattern],
    include_res: List[re.Pattern],
) -> None:
    bag_name = sanitize_filename(Path(bag_path).name)
    bag_out = out_root / bag_name
    ensure_dir(bag_out)

    # pass 1: metadata
    per_topic, total_count, first_t, last_t = first_pass_metadata(
        bag_path, exclude_res, include_res
    )
    md_txt = bag_out / "metadata.txt"
    md_json = bag_out / "metadata.json"

    duration_s = ns_to_s(last_t - first_t) if last_t > first_t else 0.0
    with md_txt.open("w") as f:
        f.write(f"Bag: {bag_path}\n")
        f.write(f"Start: {ns_to_dt(first_t).isoformat() if first_t else 'N/A'}\n")
        f.write(f"End:   {ns_to_dt(last_t).isoformat() if last_t else 'N/A'}\n")
        f.write(f"Duration [s]: {duration_s:.6f}\n")
        f.write(f"Total messages (included): {total_count}\n\n")
        f.write("Topics:\n")
        for t, md in sorted(per_topic.items()):
            f.write(f"  - {t}\n")
            f.write(f"      type: {md['type']}\n")
            f.write(f"      count: {md['count']}\n")
            f.write(f"      first_ns: {md['first_t']}\n")
            f.write(f"      last_ns:  {md['last_t']}\n")

    md_json.write_text(json.dumps({
        "bag": bag_path,
        "start_ns": first_t,
        "end_ns": last_t,
        "duration_s": duration_s,
        "total_messages": total_count,
        "topics": per_topic,
    }, indent=2))

    # per-topic export
    for topic, md in sorted(per_topic.items()):
        if md["count"] == 0:
            continue
        topic_file = sanitize_filename(topic.strip("/").replace("/", "__")) + ".csv"
        out_csv = bag_out / topic_file
        print(f"  -> {topic} ({md['type']}) count={md['count']}")
        header = collect_topic_header_keys(bag_path, topic)
        n = write_topic_csv(bag_path, topic, md["type"], out_csv, header)
        print(f"     wrote {n} rows -> {out_csv}")


# ---------- cli ----------

def main():
    p = argparse.ArgumentParser(description="Export ROS 2 bags to per-topic CSV with flattened messages.")
    p.add_argument("--out", required=True, help="Output directory")
    p.add_argument("--exclude", action="append", default=[], help="Regex to exclude topics (repeatable)")
    p.add_argument("--include", action="append", default=[], help="Regex to include topics (repeatable) — REQUIRED")
    p.add_argument("bags", nargs="+", help="ROS 2 bag directories")
    args = p.parse_args()

    include_res = [re.compile(x) for x in args.include]
    exclude_res = [re.compile(x) for x in args.exclude]

    out_root = Path(args.out)
    ensure_dir(out_root)

    if not include_res:
        print("No --include patterns provided. Nothing will be exported.\n"
              "Tip: run `ros2 bag info <bag>` and then rerun with e.g. --include \"^/topic$\"")
        return

    for bag in args.bags:
        if not os.path.isdir(bag):
            print(f"[WARN] Skipping '{bag}' (not a directory)")
            continue
        print(f"\n=== Processing {bag} ===")
        export_bag(bag, out_root, exclude_res, include_res)

    print("\nDone.")


if __name__ == "__main__":
    main()
