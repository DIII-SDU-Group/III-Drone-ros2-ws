"""Bag-derived signals for the paper's HIL soak figures (needs ROS: rosbag2_py,
px4_msgs, iii_drone_interfaces). Results are cached as .npz under the cache
directory, keyed by the bag folder and its files' sizes, so a re-run only
reads new or grown bags.

Whole-run series (one pass per bag folder):
  battery   /hil/px4_battery_status at 1 Hz: epoch, voltage_v, remaining
  odometry  receipt times of /fmu/out/vehicle_odometry at the Pi's recorder:
            inter-arrival gap quantiles and counts above thresholds
Window signals (Figure 2): applied wind, controller reference, PX4 odometry,
ground truth and gripper status inside a short time window.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _rel(path: pathlib.Path) -> str:
    """Workspace-relative path: the same key inside the devcontainer and on the host."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())

BATTERY_TOPIC = "/hil/px4_battery_status"
ODOMETRY_TOPIC = "/fmu/out/vehicle_odometry"
GAP_QUANTILES = (0.5, 0.9, 0.99, 0.999, 1.0)
GAP_THRESHOLDS_S = (0.05, 0.1, 0.25)


def _bag_files(bag_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(bag_dir.glob("*.mcap"), key=lambda p: int(p.stem.rsplit("_", 1)[-1]))


def _key(bag_dir: pathlib.Path, extra: str = "") -> str:
    sizes = ",".join(f"{p.name}:{p.stat().st_size}" for p in _bag_files(bag_dir))
    return hashlib.sha1(f"{_rel(bag_dir)}|{sizes}|{extra}".encode()).hexdigest()[:16]


def _readers(bag_dir: pathlib.Path, topics: list[str]):
    """One reader for a closed bag folder (metadata present), else one per file
    (a bag still being written has no index or metadata)."""
    import rosbag2_py

    def open_uri(uri: str):
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=uri, storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
        available = {t.name for t in reader.get_all_topics_and_types()}
        wanted = [t for t in topics if t in available]
        if not wanted:
            return None
        reader.set_filter(rosbag2_py.StorageFilter(topics=wanted))
        return reader

    if (bag_dir / "metadata.yaml").exists():
        reader = open_uri(str(bag_dir))
        return [reader] if reader else []
    readers = []
    for path in _bag_files(bag_dir):
        reader = open_uri(str(path))
        if reader:
            readers.append(reader)
    return readers


def run_series(bag_dir: pathlib.Path, cache: pathlib.Path) -> dict:
    """Battery (1 Hz) and odometry receipt-gap statistics of one bag folder."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"series_{bag_dir.name}_{_key(bag_dir, 'v1')}.npz"
    if path.exists():
        data = np.load(path, allow_pickle=False)
        return {key: data[key] for key in data.files}
    from rclpy.serialization import deserialize_message
    from px4_msgs.msg import BatteryStatus

    battery, last_kept = [], -1.0
    odometry_times = []
    for reader in _readers(bag_dir, [BATTERY_TOPIC, ODOMETRY_TOPIC]):
        while reader.has_next():
            topic, raw, t_ns = reader.read_next()
            t = t_ns * 1e-9
            if topic == ODOMETRY_TOPIC:
                odometry_times.append(t)
            elif t - last_kept >= 1.0:
                msg = deserialize_message(raw, BatteryStatus)
                battery.append((t, float(msg.voltage_v), float(msg.remaining)))
                last_kept = t
    times = np.sort(np.asarray(odometry_times, dtype=np.float64))
    gaps = np.diff(times) if times.size > 1 else np.zeros(0)
    # A new bag file or folder starts after a recording pause; those gaps are
    # not transport gaps, so gaps longer than 30 s are left out.
    gaps = gaps[gaps < 30.0]
    result = {
        "battery": np.asarray(battery, dtype=np.float64).reshape(-1, 3),
        "odometry_count": np.asarray(times.size),
        "gap_quantiles": np.quantile(gaps, GAP_QUANTILES) if gaps.size else np.full(len(GAP_QUANTILES), np.nan),
        "gap_over": np.asarray([(gaps > th).sum() for th in GAP_THRESHOLDS_S]),
        "gap_hist": np.histogram(gaps, bins=np.logspace(-3, 1.5, 91))[0] if gaps.size else np.zeros(90, int),
    }
    np.savez(path, **result)
    return result


WINDOW_TOPICS = {
    "wind": "/simulation/wind/velocity",
    "clock": "/clock",
    "reference": "/control/maneuver_controller/reference",
    "odometry": ODOMETRY_TOPIC,
    "truth": "/simulation/ground_truth/drone/state",
    "gripper": "/payload/charger_gripper/gripper_status",
}


def window_signals(bag_dirs: list[pathlib.Path], t0: float, t1: float, cache: pathlib.Path) -> dict:
    """Figure 2's context signals inside [t0, t1] (epoch seconds, receipt time)."""
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(json.dumps([_rel(d) for d in bag_dirs] + [round(t0, 3), round(t1, 3)]).encode()).hexdigest()[:16]
    path = cache / f"window_{key}.npz"
    if path.exists():
        data = np.load(path, allow_pickle=False)
        return {k: data[k] for k in data.files}
    from rclpy.serialization import deserialize_message
    from rosgraph_msgs.msg import Clock
    from geometry_msgs.msg import Vector3Stamped
    from px4_msgs.msg import VehicleOdometry
    from iii_drone_interfaces.msg import GripperStatus, Reference, SimulatorDroneState

    types = {"wind": Vector3Stamped, "clock": Clock, "reference": Reference, "odometry": VehicleOdometry,
             "truth": SimulatorDroneState, "gripper": GripperStatus}
    by_topic = {topic: name for name, topic in WINDOW_TOPICS.items()}
    rows: dict[str, list] = {name: [] for name in WINDOW_TOPICS}
    last: dict[str, float] = {}
    period = {"clock": 0.02, "odometry": 0.02, "truth": 0.02, "gripper": 0.05}
    for bag_dir in bag_dirs:
        for reader in _readers(bag_dir, list(WINDOW_TOPICS.values())):
            if (bag_dir / "metadata.yaml").exists():
                reader.seek(int(t0 * 1e9))
            while reader.has_next():
                topic, raw, t_ns = reader.read_next()
                t = t_ns * 1e-9
                if t < t0:
                    continue
                if t > t1:
                    if (bag_dir / "metadata.yaml").exists():
                        break
                    continue
                name = by_topic[topic]
                if t - last.get(name, -1e9) < period.get(name, 0.0):
                    continue
                last[name] = t
                msg = deserialize_message(raw, types[name])
                if name == "wind":
                    rows[name].append((t, msg.vector.x, msg.vector.y, msg.vector.z))
                elif name == "clock":
                    rows[name].append((t, msg.clock.sec + msg.clock.nanosec * 1e-9))
                elif name == "reference":
                    rows[name].append((t, msg.position.x, msg.position.y, msg.position.z))
                elif name == "odometry":
                    rows[name].append((t, *map(float, msg.position)))
                elif name == "truth":
                    p = msg.pose_world.position
                    rows[name].append((t, p.x, p.y, p.z))
                elif name == "gripper":
                    rows[name].append((t, float(msg.gripper_status)))
    width = {"wind": 4, "clock": 2, "reference": 4, "odometry": 4, "truth": 4, "gripper": 2}
    result = {name: np.asarray(sorted(v), dtype=np.float64).reshape(-1, width[name]) for name, v in rows.items()}
    np.savez(path, **result)
    return result
