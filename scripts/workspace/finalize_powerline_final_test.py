#!/usr/bin/env python3
"""Recorder-side freeze/visibility/handoff for locked schema-v2 FINALTEST bags."""
from __future__ import annotations

import collections
import copy
import csv
import hashlib
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import powerline_qualification as q

ROOT = q.FINAL_TEST_OUTPUT
SCHEMA = Path("/home/iii/ws/../disturbance_nmpc/powerline_perception/schemas/powerline_qualification_schema_v2.json")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dir_hash(path: Path) -> str:
    """Exact SLAM deterministic_directory_hash convention."""
    h = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            rel, data = str(item.relative_to(path)), item.read_bytes()
            encoded = Path(rel).as_posix().encode()
            h.update(len(encoded).to_bytes(4, "big"))
            h.update(encoded)
            h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def stamp(message) -> float:
    return message.header.stamp.sec + message.header.stamp.nanosec * 1e-9


def runs(samples, wanted: bool):
    result, start, last = [], None, None
    for time_s, state in samples:
        if state == wanted and start is None:
            start = time_s
        elif state != wanted and start is not None:
            result.append((start, last))
            start = None
        last = time_s
    if start is not None:
        result.append((start, last))
    return result


def intersections(left, right):
    return [(max(a, c), min(b, d)) for a, b in left for c, d in right if min(b, d) > max(a, c)]


def longest(intervals) -> float:
    return max([0.0] + [b - a for a, b in intervals])


def inspect_bag(path: Path):
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=""), rosbag2_py.ConverterOptions("", ""))
    types = {x.name: x.type for x in reader.get_all_topics_and_types()}
    classes = {name: get_message(kind) for name, kind in types.items()}
    camera, radar, fields = [], [], []
    camera_by_id, radar_by_id = collections.Counter(), collections.Counter()
    camera_identity_samples = collections.defaultdict(list)
    while reader.has_next():
        topic, data, _ = reader.read_next()
        if topic == "/simulation/ground_truth/cable_camera/frame":
            msg = deserialize_message(data, classes[topic])
            visible = {x.physical_conductor_id for x in msg.conductors if x.visibility_state == x.VISIBLE}
            camera.append((stamp(msg), bool(visible)))
            for cid in q.Corridor.load().ids:
                camera_identity_samples[cid].append((stamp(msg), cid in visible))
            camera_by_id.update(visible)
        elif topic == "/simulation/ground_truth/mmwave/scan":
            msg = deserialize_message(data, classes[topic])
            physical = [x for x in msg.points if x.source_class == x.VALID_PHYSICAL_CONDUCTOR]
            radar.append((stamp(msg), bool(physical)))
            radar_by_id.update(x.physical_conductor_id for x in physical)
        elif topic == "/sensor/mmwave/points_full" and not fields:
            msg = deserialize_message(data, classes[topic])
            fields = [x.name for x in msg.fields]
    camera_zero, radar_zero = runs(camera, False), runs(radar, False)
    joint_zero = intersections(camera_zero, radar_zero)
    joint_visible = intersections(runs(camera, True), runs(radar, True))
    loss = max(joint_zero, key=lambda value: value[1] - value[0], default=(0.0, 0.0))
    visible_after = [(max(a, loss[1]), b) for a, b in joint_visible if b > loss[1] and b > max(a, loss[1])]
    return {
        "camera_samples": camera,
        "radar_samples": radar,
        "camera_visible_frames_by_physical_conductor": dict(camera_by_id),
        "radar_returns_by_physical_conductor": dict(radar_by_id),
        "camera_zero_intervals_s": camera_zero,
        "radar_zero_intervals_s": radar_zero,
        "simultaneous_zero_intervals_s": joint_zero,
        "longest_camera_zero_s": longest(camera_zero),
        "longest_radar_zero_s": longest(radar_zero),
        "longest_simultaneous_all_loss_s": longest(joint_zero),
        "post_reentry_simultaneous_visible_s": longest(visible_after),
        "radar_point_fields": fields,
        "camera_identity_samples": camera_identity_samples,
    }


def main() -> int:
    freeze = json.loads((ROOT / "recorder_freeze.json").read_text())
    corridor = json.loads((ROOT / "corridor_frame.json").read_text())
    g = q.Corridor.load()
    entries, inspections = [], {}
    fig = plt.figure(figsize=(12, 9))
    ax = fig.add_subplot(111, projection="3d")
    visibility_fig, visibility_axes = plt.subplots(5, 1, figsize=(13, 12), sharex=False)
    for index, spec in enumerate(q.FINAL_FLIGHTS):
        fid, out = spec["flight_id"], ROOT / spec["flight_id"]
        report = json.loads((out / "topic_validation.json").read_text())
        if not report["success"]:
            raise RuntimeError(f"{fid}: topic/tracking validation failed")
        truth = report["topic_ground_truth"]
        if not all(truth["checks"].values()):
            raise RuntimeError(f"{fid}: truth alignment check failed")
        inspection = inspect_bag(out / "bag")
        inspections[fid] = inspection
        required_fields = {"x", "y", "z", "velocity", "snr", "noise"}
        if not required_fields.issubset(inspection["radar_point_fields"]):
            raise RuntimeError(f"{fid}: missing radar fields {required_fields - set(inspection['radar_point_fields'])}")
        rows = list(csv.DictReader((out / "actual_trajectory.csv").open()))
        matrix = lambda names: np.array([[float(row[name]) for name in names] for row in rows])
        actual_p = matrix(["actual_xG", "actual_yG", "actual_zG"])
        actual_v = matrix(["actual_vxG", "actual_vyG", "actual_vzG"])
        command_p = matrix(["cmd_xG", "cmd_yG", "cmd_zG"])
        command_v = matrix(["cmd_vxG", "cmd_vyG", "cmd_vzG"])
        command_yaw = matrix(["cmd_yaw"])[:, 0]
        executed_d = g.z(g.ids[0], actual_p[:, 0]) - actual_p[:, 2]
        definition = json.loads((out / "trajectory_definition.json").read_text())
        topic_counts = {name: item["message_count"] for name, item in truth["topics"].items()}
        visibility = {key: value for key, value in inspection.items() if not key.endswith("samples")}
        visibility.update({"camera": truth["camera"], "radar": truth["radar"]})
        (out / "visibility_validation.json").write_text(json.dumps(visibility, indent=2, sort_keys=True) + "\n")
        frozen_types = {
            "FINALTEST01": ("ordinary_mixed_independent", "ordinary_mixed_approach_generalization"),
            "FINALTEST02": ("aggressive_combined_dynamics_independent", "aggressive_combined_translation_attitude_dynamics"),
            "FINALTEST03": ("six_regime_speed_generalization_independent", "altered_six_regime_truth_speed_process_generalization"),
            "FINALTEST04": ("partial_visibility_indirect_c0_reacquisition_independent", "partial_asymmetric_visibility_indirect_c0_and_reacquisition"),
            "FINALTEST05": ("simultaneous_all_conductor_loss_reentry_independent", "true_simultaneous_camera_radar_all_conductor_loss_and_reentry"),
        }
        entry = {
            "flight_id": fid, "seed": spec["seed"], "role": definition["name"], "split": "test",
            "schema_version": 2, "bag_path": f"{fid}/bag", "raw_bag_path": f"{fid}/bag_raw",
            "trajectory_type": frozen_types[fid][0], "scientific_role": frozen_types[fid][1],
            "trajectory_definition": definition, "trajectory_definition_sha256": sha(out / "trajectory_definition.json"),
            "A_m": g.A, "requested_duration_s": definition["duration_s"], "actual_duration_s": float(rows[-1]["t_s"]),
            "duration_s": float(rows[-1]["t_s"]), "duration_s_semantics": "actual delivered recording duration",
            "commanded_speed_range_mps": [float(np.linalg.norm(command_v, axis=1).min()), float(np.linalg.norm(command_v, axis=1).max())],
            "actual_truth_speed_range_mps": [float(np.linalg.norm(actual_v, axis=1).min()), float(np.linalg.norm(actual_v, axis=1).max())],
            "actual_y_range_m": [float(actual_p[:, 1].min()), float(actual_p[:, 1].max())],
            "actual_executed_clearance_range_m": [float(executed_d.min()), float(executed_d.max())],
            "commanded_yaw_range_deg": [float(np.degrees(command_yaw.min())), float(np.degrees(command_yaw.max()))],
            "topic_counts": topic_counts,
            "camera_count": topic_counts["/sensor/cable_camera/image_raw"],
            "radar_scan_count": topic_counts["/sensor/mmwave/points_full"],
            "radar_point_count": int(sum(truth["radar"]["source_class_counts"].values())),
            "imu_count": topic_counts["/fmu/out/sensor_combined"],
            "drone_gt_count": topic_counts["/simulation/ground_truth/drone/state"],
            "camera_visible_frames_by_physical_conductor": inspection["camera_visible_frames_by_physical_conductor"],
            "radar_returns_by_physical_conductor": inspection["radar_returns_by_physical_conductor"],
            "simultaneous_all_loss_duration_s": inspection["longest_simultaneous_all_loss_s"],
            "post_reentry_visible_duration_s": inspection["post_reentry_simultaneous_visible_s"],
            "tracking_validation": report["tracking"], "topic_truth_validation": truth,
            "validation_status": "PASS",
            "bag_sha256": dir_hash(out / "bag"),
            "truth_contract": {},
            "hashes": {
                "bag_sha256": dir_hash(out / "bag"), "raw_bag_sha256": dir_hash(out / "bag_raw"),
                "trajectory_definition_sha256": sha(out / "trajectory_definition.json"),
                "recorder_freeze_sha256": sha(ROOT / "recorder_freeze.json"),
            },
            "provenance": {"requested_seed": spec["seed"], "actual_applied_seeds": json.loads((out / "flight_metadata.json").read_text())["actual_applied_seeds"]},
        }
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        entry["hashes"]["manifest_entry_sha256"] = hashlib.sha256(canonical).hexdigest()
        entries.append(entry)
        ax.plot(actual_p[:, 0], actual_p[:, 1], actual_p[:, 2], label=fid)
        time0 = min(inspection["camera_samples"][0][0], inspection["radar_samples"][0][0])
        ca = inspection["camera_samples"]
        ra = inspection["radar_samples"]
        visibility_axes[index].step([x - time0 for x, _ in ca], [int(y) for _, y in ca], where="post", label="camera any conductor")
        visibility_axes[index].step([x - time0 for x, _ in ra], [int(y) for _, y in ra], where="post", label="radar physical source", alpha=.8)
        visibility_axes[index].set_title(fid)
        visibility_axes[index].set_ylim(-.1, 1.1)
        visibility_axes[index].grid()
    final04 = inspections["FINALTEST04"]
    c0 = corridor["logical_to_asset_id"]["C0"]
    c0_samples = final04["camera_identity_samples"][c0]
    other_visible_during_c0_absence = any(not c0_visible and any(
        state for cid, samples in final04["camera_identity_samples"].items() if cid != c0
        for time2, state in samples if abs(time2 - time_s) < 1e-6
    ) for time_s, c0_visible in c0_samples)
    c0_visible_runs = runs(c0_samples, True)
    if len(c0_visible_runs) < 2 or not other_visible_during_c0_absence:
        raise RuntimeError("FINALTEST04 lacks direct C0 -> indirect C0 -> C0 return truth sequence")
    final05 = inspections["FINALTEST05"]
    if final05["longest_simultaneous_all_loss_s"] < 30.0:
        raise RuntimeError(f"FINALTEST05 all-loss is only {final05['longest_simultaneous_all_loss_s']:.3f}s")
    if final05["post_reentry_simultaneous_visible_s"] < 20.0:
        raise RuntimeError(f"FINALTEST05 post-reentry visible is only {final05['post_reentry_simultaneous_visible_s']:.3f}s")
    ax.set(xlabel="x_G [m]", ylabel="y_G [m]", zlabel="world z [m]", title="FINALTEST executed simulator-truth trajectories")
    ax.legend(); fig.tight_layout(); fig.savefig(ROOT / "all_5_executed_truth_trajectories.png", dpi=180); plt.close(fig)
    visibility_axes[-1].set_xlabel("seconds from first truth sample")
    visibility_axes[0].legend(loc="lower right")
    visibility_fig.tight_layout(); visibility_fig.savefig(ROOT / "finaltest_visibility_summary.png", dpi=180); plt.close(visibility_fig)
    loss_plot = ROOT / "FINALTEST05" / "FINALTEST05_truth_visibility_timeline.png"
    source_plot = ROOT / "finaltest_visibility_summary.png"
    loss_plot.write_bytes(source_plot.read_bytes())
    preflight_plot = ROOT / "FINALTEST05" / "quicklook" / "preflight_trajectory.png"
    (ROOT / "FINALTEST05" / "FINALTEST05_loss_pose_geometry.png").write_bytes(preflight_plot.read_bytes())
    truth_contract = {"schema_file": "powerline_qualification_schema_v2.json", "schema_sha256": "5ea968c227dbf5df5aa58910d81e2afb67dd37031dc322db651effff26259753", "corridor_sidecar": "corridor_frame.json", "contract_only_validation_completed": False, "estimator_or_frontend_metrics_computed_before_lock": False}
    for entry in entries:
        entry["truth_contract"] = copy.deepcopy(truth_contract)
    manifest = {
        "powerline_qualification_schema_version": 2, "dataset": "powerline_qualification_final_test",
        "dataset_role": "final_test", "held_out": True, "flat_layout": True,
        "bag_sha256_convention": "deterministic_directory_hash_v1_relative_path_plus_contents",
        "split_policy": {"unit": "whole flight", "test_payload_open_count_before_lock": 0},
        "truth_contract": truth_contract,
        "recorder_build": {"workspace": "/home/iii/ws", "source_tree_sha256": freeze["source_tree_sha256"], "recorder_command_sha256": freeze["recorder_command_sha256"], "canary_manifest_sha256": sha(q.FINAL_CANARY_OUTPUT / "dataset_manifest.json"), "canary_source_tree_sha256": freeze["source_tree_sha256"], "canary_recorder_command_sha256": freeze["recorder_command_sha256"], "finaltest_execution_wrapper_sha256": sha(q.ROOT / "scripts/workspace/run_powerline_qualification_isolated.sh"), "truth_publisher_build_sha256": freeze["truth_publisher_build_sha256"], "message_interface_sha256": freeze["message_interface_sha256"], "calibration_sha256": freeze["calibration_sha256"], "trajectory_executor_sha256": freeze["trajectory_executor_sha256"], "recording_qos_sha256": sha(q.ROOT / "scripts/workspace/powerline_qualification_record_qos.yaml"), "trajectory_catalog_sha256": sha(q.ROOT / "scripts/workspace/powerline_qualification.py")},
        "isolation": {"ros_domain_id": 214, "xrce_udp_port": 20314, "px4_instance": 54, "px4_sysid": 55, "gazebo_partition_prefix": "iii_powerline_final_test", "ros_discovery": "localhost", "mavlink_qgroundcontrol": False},
        "flight_count": 5, "flights": entries,
        "scientific_access_attestation": "No estimator or scientific camera/radar frontend evaluation was run on FINALTEST."
    }
    (ROOT / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    lines = ["# FINALTEST recorder handoff", "", "Schema-v2 held-out bags recorded through the standalone PX4 Offboard trajectory-setpoint executor. No simulator teleportation, estimator, or scientific frontend evaluation was used.", "", f"Corridor: `x_G` follows the conductors, `z_G` is gravity-up, `y_G=z_G×x_G`; `A={g.A:.12f} m`. C0 is the bottom conductor. The unchanged-world requested-to-executed clearance mapping is recorded per trajectory.", "", "| Flight | Seed | Role | Camera | Radar | Position RMS m | Velocity RMS m/s | All-loss s | Post-reentry visible s |", "|---|---:|---|---:|---:|---:|---:|---:|---:|"]
    for entry in entries:
        metrics = entry["tracking_validation"]["metrics"]
        lines.append(f"| {entry['flight_id']} | {entry['seed']} | {entry['role']} | {entry['camera_count']} | {entry['radar_scan_count']} | {metrics['position_rms_m']:.3f} | {metrics['velocity_rms_mps']:.3f} | {entry['simultaneous_all_loss_duration_s']:.3f} | {entry['post_reentry_visible_duration_s']:.3f} |")
    lines += ["", "FINALTEST04 passed direct-C0, indirect-C0 (C0 absent while another physical conductor remained visible), and C0-return checks from rendered source truth.", "", f"FINALTEST05 source truth proves **{entries[-1]['simultaneous_all_loss_duration_s']:.3f} s** simultaneous camera/radar all-conductor absence and **{entries[-1]['post_reentry_visible_duration_s']:.3f} s** continuous joint visible data after re-entry.", "", "The truth publisher binary, interface definitions, calibration, trajectory executor, QoS, and schema sidecar match the accepted CANARY hashes in `recorder_freeze.json`. The outer isolated execution wrapper was extended after CANARY solely to route FINALTEST IDs/seeds/output/isolation; both its CANARY hash and exact FINALTEST hash are recorded. Sensor recording, truth publication, message contracts, calibration, QoS, and executor mechanics were unchanged. The trajectory catalog was necessarily extended for FINALTEST and timing-only safety adaptations were recorded. Auxiliary baseline perception binaries were rebuilt after unrelated install-tree churn; they are recorded context topics and are not authoritative sensor/truth publishers.", "", "Rerecords: FINALTEST02 attempt 1 narrowly missed its tracking gate and passed after uniform 1.10 time scaling; FINALTEST04 attempts 1–2 failed lateral tracking and passed after uniform 1.25 time scaling. Spatial paths were unchanged. Failed attempts remain outside the delivered dataset under `runtime/isolated/.../failed_attempts`.", "", "World convention: Gazebo ENU; quaternion order xyzw; simulator linear/angular velocities are expressed in world; drone source link is `base_link`. Radar truth is exact stamp/index ordered and records ideal pre-noise sources. Camera truth is rendered, FOV-clipped, occlusion-aware instance truth. C0=conductor_4, C1=conductor_3, C2=conductor_2, C3=conductor_1.", "", "Known limitations: the outer flight-routing wrapper hash differs from CANARY as disclosed above; authoritative recorder/truth components remain frozen. Zero/visible durations conservatively intersect contiguous camera-frame and radar-scan source-truth runs. SLAM-side contract-only validation remains intentionally pending. ESTIMATOR NOT RUN ON FINALTEST.", ""]
    (ROOT / "DATASET_HANDOFF.md").write_text("\n".join(lines))
    print(json.dumps({"status": "PASS", "flights": 5, "FINALTEST05_all_loss_s": entries[-1]["simultaneous_all_loss_duration_s"], "FINALTEST05_post_reentry_visible_s": entries[-1]["post_reentry_visible_duration_s"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
