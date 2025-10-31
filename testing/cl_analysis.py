#!/usr/bin/env python3
import argparse
import os
import math as py_math
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.legend_handler import HandlerBase
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.ticker as mticker
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (needed for 3D)
import csv

import rosbag2_py
from rclpy.serialization import deserialize_message

from tf2_msgs.msg import TFMessage
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped
from px4_msgs.msg import VehicleOdometry
from iii_drone_interfaces.msg import StringStamped, Maneuver, Powerline, SingleLine

# Your internal util; assumed available
from iii_drone_core.utils import math as iii_math  # for quatToMat

TOPIC_TYPE_MAP = {
    '/tf': TFMessage,
    '/control/trajectory_generator/trajectory_path': Path,
    '/control/trajectory_generator/target_pose': PoseStamped,
    '/fmu/out/vehicle_odometry': VehicleOdometry,
    '/mission/mission_executor/maneuver_reference_client/reference_mode': StringStamped,
    '/control/maneuver_controller/current_maneuver': Maneuver,
    '/perception/pl_mapper/powerline': Powerline,
    # "/control/maneuver_controller/reference_callback_provider": StringStamped
}

def quat_wxyz_to_R(q):
    """
    q: (..., 4) quaternion in [w, x, y, z] with arbitrary scale
    returns: (..., 3, 3) rotation matrices (world <- drone)
    """
    q = np.asarray(q, dtype=float)
    w, x, y, z = np.moveaxis(q / np.linalg.norm(q, axis=-1, keepdims=True), -1, 0)
    # rotation matrix from quaternion (Hamilton convention)
    ww, xx, yy, zz = w*w, x*x, y*y, z*z
    wx, wy, wz = w*x, w*y, w*z
    xy, xz, yz = x*y, x*z, y*z
    R = np.empty(q.shape[:-1] + (3, 3), dtype=float)
    R[..., 0, 0] = 1 - 2*(yy + zz)
    R[..., 0, 1] = 2*(xy - wz)
    R[..., 0, 2] = 2*(xz + wy)
    R[..., 1, 0] = 2*(xy + wz)
    R[..., 1, 1] = 1 - 2*(xx + zz)
    R[..., 1, 2] = 2*(yz - wx)
    R[..., 2, 0] = 2*(xz - wy)
    R[..., 2, 1] = 2*(yz + wx)
    R[..., 2, 2] = 1 - 2*(xx + yy)
    return R

# ----------------------------
# Data container
# ----------------------------

class Data:
    def __init__(self, topic_lists: dict):
        self.tf_msgs = topic_lists['/tf']
        self.traj_msgs = topic_lists['/control/trajectory_generator/trajectory_path']
        self.target_msgs = topic_lists['/control/trajectory_generator/target_pose']
        self.odom_msgs = topic_lists['/fmu/out/vehicle_odometry']
        self.refmode_msgs = topic_lists['/mission/mission_executor/maneuver_reference_client/reference_mode']
        self.maneuver_msgs = topic_lists['/control/maneuver_controller/current_maneuver']
        self.powerline_msgs = topic_lists['/perception/pl_mapper/powerline']

        self.tf_xyz, self.tf_qwxyz, self.tf_t = self._tf_to_numpy(self.tf_msgs)
        self.cmd_xyz = self._path_to_numpy(self.traj_msgs)               # commanded reference point vs time
        self.tgt_xyz, self.tgt_t = self._pose_to_numpy(self.target_msgs)
        self.odom_xyz, self.odom_t = self._odom_to_numpy(self.odom_msgs)
        self.powerline_xyz, self.powerline_t = self._pl_to_numpy(self.powerline_msgs)
        self.powerline_xyz, _ = self._powerlines_drone_to_world(
            self.tf_xyz, self.tf_qwxyz, self.tf_t,
            self.powerline_xyz, self.powerline_t
        )

        print(f"Data loaded: {len(self.tf_msgs)} TF msgs, {len(self.traj_msgs)} traj msgs, "
              f"{len(self.target_msgs)} target msgs, {len(self.odom_msgs)} odom msgs, "
              f"{len(self.refmode_msgs)} refmode msgs, {len(self.maneuver_msgs)} maneuver msgs, "
              f"{len(self.powerline_msgs)} powerline msgs")
        print(f"Powerlines shape: {self.powerline_xyz.shape}")

    @staticmethod
    def _tf_to_numpy(tf_msgs):
        xyz, qwxyz, t = [], [], []
        for m in tf_msgs:
            tr = m.transforms[0]
            xyz.append([tr.transform.translation.x,
                        tr.transform.translation.y,
                        tr.transform.translation.z])
            qwxyz.append([tr.transform.rotation.w,
                          tr.transform.rotation.x,
                          tr.transform.rotation.y,
                          tr.transform.rotation.z])
            t.append(tr.header.stamp.sec + tr.header.stamp.nanosec * 1e-9)
        return np.asarray(xyz), np.asarray(qwxyz), np.asarray(t)

    @staticmethod
    def _path_to_numpy(path_msgs):
        """
        We take the *first* pose in each Path message as the commanded reference
        at that instant (common pattern for streaming a moving reference).
        """
        xyz = []
        for m in path_msgs:
            if not m.poses:
                continue
            p = m.poses[0].pose.position
            xyz.append([p.x, p.y, p.z])
        return np.asarray(xyz)

    @staticmethod
    def _pose_to_numpy(pose_msgs):
        xyz, t = [], []
        for m in pose_msgs:
            p = m.pose.position
            xyz.append([p.x, p.y, p.z])
            t.append(m.header.stamp.sec + m.header.stamp.nanosec * 1e-9)
        return np.asarray(xyz), np.asarray(t)

    @staticmethod
    def _odom_to_numpy(odom_msgs):
        xyz, t = [], []
        for m in odom_msgs:
            xyz.append([m.position[0], m.position[1], m.position[2]])
            t.append(m.timestamp * 1e-6)  # microseconds -> seconds
        return np.asarray(xyz), np.asarray(t)

    @staticmethod
    def _pl_to_numpy(powerline_msgs: list[Powerline]):
        max_n_lines = max((len(m.lines) for m in powerline_msgs), default=0)
        xyz, t = [], []
        for m in powerline_msgs:
            positions = []
            for line in m.lines:
                positions.append(np.array([line.pose.position.x, line.pose.position.y, line.pose.position.z]))
            
            # Sort positions by ascending z (lowest first)
            positions = np.array(sorted(positions, key=lambda p: p[2]))
            # Pad with NaNs if fewer lines than max
            while positions.shape[0] < max_n_lines:
                positions = np.vstack([positions, np.array([np.nan, np.nan, np.nan])])
            xyz.append(positions)
            t.append(m.stamp.sec + m.stamp.nanosec * 1e-9)
        return np.asarray(xyz), np.asarray(t)

    @staticmethod
    def _powerlines_drone_to_world(tf_xyz, tf_qwxyz, tf_t, powerline_xyz, powerline_t):
        tf_xyz   = np.asarray(tf_xyz, dtype=float)
        tf_qwxyz = np.asarray(tf_qwxyz, dtype=float)
        tf_t     = np.asarray(tf_t, dtype=float)
        powerline_xyz = np.asarray(powerline_xyz, dtype=float)
        powerline_t   = np.asarray(powerline_t, dtype=float)

        # sort transforms by time
        order = np.argsort(tf_t)
        tf_t_sorted = tf_t[order]
        tf_xyz_sorted = tf_xyz[order]
        tf_q_sorted = tf_qwxyz[order]

        # find most recent transform index
        idx = np.searchsorted(tf_t_sorted, powerline_t, side='right') - 1
        valid = idx >= 0

        M, L = powerline_xyz.shape[:2]
        world_xyz = np.full_like(powerline_xyz, np.nan)

        if np.any(valid):
            Rm = quat_wxyz_to_R(tf_q_sorted[idx[valid]])        # (M_valid, 3, 3)
            tm = tf_xyz_sorted[idx[valid]]                      # (M_valid, 3)
            world_xyz_valid = np.einsum('mij,mlj->mli', Rm, powerline_xyz[valid]) + tm[:, None, :]

            # --- sort by z-value per frame ---
            for mi, frame in enumerate(world_xyz_valid):
                order = np.argsort(frame[:, 2])  # sort by z (3rd coord)
                world_xyz_valid[mi] = frame[order]

            world_xyz[valid] = world_xyz_valid

        return world_xyz, idx

# ----------------------------
# ROS2 bag loading
# ----------------------------

def get_readers(dir_path: str):
    bag_dirs = sorted(os.listdir(dir_path))
    readers = []
    for d in bag_dirs:
        uri = os.path.join(dir_path, d)
        if not os.path.isdir(uri):
            continue
        r = rosbag2_py.SequentialReader()
        storage_options = rosbag2_py._storage.StorageOptions(uri=uri)
        converter_options = rosbag2_py._storage.ConverterOptions('', '')
        r.open(storage_options, converter_options)
        readers.append(r)
    return readers

def extract_segment(reader: rosbag2_py.SequentialReader) -> Data:
    hbo_topic_lists = {
        key: [] for key in TOPIC_TYPE_MAP.keys()
    }
    
    cl_topic_lists = {
        key: [] for key in TOPIC_TYPE_MAP.keys()
    }

    latest_tf = latest_tp = latest_odom = latest_ref = latest_pl = None
    hbo_started = False
    cl_started = False

    while reader.has_next():
        topic, serialized, _ = reader.read_next()
        if topic not in hbo_topic_lists:
            continue
        msg = deserialize_message(serialized, TOPIC_TYPE_MAP[topic])
        
        # if not started
        
        # if topic == "/control/maneuver_controller/reference_callback_provider":
        #     mode = msg.data
        #     if mode != last_mode:
        #         print(f"Mode change: {last_mode} -> {mode}")
        #         last_mode = mode

        if hbo_started:
            # stop when we hit hover
            # if topic == '/control/maneuver_controller/reference_callback_provider' and msg.data == 'cable_landing':
            if topic == '/control/maneuver_controller/current_maneuver' and msg.maneuver_type == Maneuver.MANEUVER_TYPE_CABLE_LANDING:
                hbo_started = False
                cl_started = True
                continue

            if topic == '/tf':
                tr = msg.transforms[0]
                if not (tr.child_frame_id == 'drone' and tr.header.frame_id == 'world'):
                    continue

            hbo_topic_lists[topic].append(msg)
            
        elif cl_started:
            # if topic == '/control/maneuver_controller/reference_callback_provider' and msg.data != 'cable_landing':
            if topic == '/control/maneuver_controller/current_maneuver' and msg.maneuver_type != Maneuver.MANEUVER_TYPE_CABLE_LANDING:
                hbo_started = False
                cl_started = False
                break
            
            if topic == '/tf':
                tr = msg.transforms[0]
                if not (tr.child_frame_id == 'drone' and tr.header.frame_id == 'world'):
                    continue

            cl_topic_lists[topic].append(msg)

        else:
            # if topic == "/control/maneuver_controller/reference_callback_provider" and msg.data == "hover_by_object":
            if topic == '/control/maneuver_controller/current_maneuver' and msg.maneuver_type == Maneuver.MANEUVER_TYPE_HOVER_BY_OBJECT:
                hbo_started = True
                # traj_path.append(msg)
                if latest_tf is not None: hbo_topic_lists["/tf"].append(latest_tf)
                if latest_tp is not None: hbo_topic_lists["/control/trajectory_generator/target_pose"].append(latest_tp)
                if latest_odom is not None: hbo_topic_lists["/fmu/out/vehicle_odometry"].append(latest_odom)
                if latest_ref is not None: hbo_topic_lists["/mission/mission_executor/maneuver_reference_client/reference_mode"].append(latest_ref)
                if latest_pl is not None: hbo_topic_lists["/perception/pl_mapper/powerline"].append(latest_pl)
            elif topic == '/tf':
                tr = msg.transforms[0]
                if tr.child_frame_id == 'drone' and tr.header.frame_id == 'world':
                    latest_tf = msg
            elif topic == '/control/trajectory_generator/target_pose':
                latest_tp = msg
            elif topic == '/fmu/out/vehicle_odometry':
                latest_odom = msg
            elif topic == '/mission/mission_executor/maneuver_reference_client/reference_mode':
                latest_ref = msg
            elif topic == '/perception/pl_mapper/powerline':
                latest_pl = msg

    return Data(cl_topic_lists)
    # return Data(hbo_topic_lists), Data(cl_topic_lists)

def load_runs(dir_path: str):
    return [extract_segment(r) for r in get_readers(dir_path)]


import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
from typing import List, Tuple, Optional

def _estimate_cable_position(run):
    """Representative cable point in WORLD coords for alignment."""
    avg_tgt = np.nanmean(run.tgt_xyz, axis=0)  # (3,)
    closest = []
    for pl_frame in run.powerline_xyz:  # (L,3) per timestamp
        valid = ~np.isnan(pl_frame).any(axis=1)
        if not np.any(valid):
            continue
        lines = pl_frame[valid]
        d = np.linalg.norm(lines - avg_tgt, axis=1)
        closest.append(lines[np.argmin(d)])
    if not closest:
        return avg_tgt
    return np.nanmean(np.stack(closest, 0), axis=0)

def _cube_limits(xs, ys, zs, pad_ratio=0.05):
    x_min, x_max = np.nanmin(xs), np.nanmax(xs)
    y_min, y_max = np.nanmin(ys), np.nanmax(ys)
    z_min, z_max = np.nanmin(zs), np.nanmax(zs)
    cx, cy, cz = (x_min+x_max)/2, (y_min+y_max)/2, (z_min+z_max)/2
    half = 0.5 * max(x_max-x_min, y_max-y_min, z_max-z_min)
    half = max(half, 1e-6) * (1.0 + pad_ratio)
    return (cx-half, cx+half), (cy-half, cy+half), (cz-half, cz+half)

def _plot_single_config_3d(config_name: str, runs: List[object]):
    """One figure for one config (3 runs). Returns (fig, ax)."""
    aligned = []
    xs, ys, zs = [], [], []
    for run in runs:
        cable = _estimate_cable_position(run)
        flown = run.tf_xyz - cable
        planned = getattr(run, 'traj_xyz', None)
        planned = (planned - cable) if planned is not None else None
        aligned.append((flown, planned))
        xs.append(flown[:,0]); ys.append(flown[:,1]); zs.append(flown[:,2])

    x_all = np.concatenate(xs) if xs else np.array([-1,1])
    y_all = np.concatenate(ys) if ys else np.array([-1,1])
    z_all = np.concatenate(zs) if zs else np.array([-1,1])
    xlim, ylim, zlim = _cube_limits(x_all, y_all, z_all, pad_ratio=0.05)

    fig = plt.figure(figsize=(7.5, 6.5)) 
    ax = fig.add_subplot(111, projection='3d')

    for i, (flown, planned) in enumerate(aligned, start=1):
        ax.plot3D(flown[:,0], flown[:,1], flown[:,2], linewidth=2, label=f"Run {i}")
        if planned is not None:
            ax.plot3D(planned[:,0], planned[:,1], planned[:,2],
                      linestyle='--', alpha=0.6, linewidth=2, label=f"Planned {i}")

    # Cable at origin
    ax.scatter3D([0.0], [0.0], [0.0], marker='o', s=120, color='red', label="Powerline")

    # Axis labels (larger font, with padding)
    ax.set_xlabel("x [m]", fontsize=14, labelpad=12)
    ax.set_ylabel("y [m]", fontsize=14, labelpad=12)
    ax.set_zlabel("z [m]", fontsize=14, labelpad=12)
    ax.set_xlim(xlim); ax.set_ylim(ylim); ax.set_zlim(zlim)

    # Equal aspect
    try:
        ax.set_box_aspect((xlim[1]-xlim[0], ylim[1]-ylim[0], zlim[1]-zlim[0]))
    except Exception:
        pass

    # Ticks every 0.5, bigger font
    for axis in [ax.xaxis, ax.yaxis, ax.zaxis]:
        axis.set_major_locator(MultipleLocator(0.5))
    ax.tick_params(axis='both', which='major', labelsize=14)

    # Legend bigger font
    ax.legend(loc='best', fontsize=14)

    plt.tight_layout()
    return fig, ax

def plot_all_cable_landings_3d(
    data: List[Tuple[str, List[object]]],
    show: bool = True,
    save_dir: Optional[str] = None,
    filename_prefix: str = "cable_landing_3d"
):
    """Iterates configs and generates one figure per config."""
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        
    figs = []
    for idx, (name, runs) in enumerate(data, start=1):
        fig, ax = _plot_single_config_3d(name, runs)
        figs.append(fig)
        if save_dir:
            path = os.path.join(save_dir, f"{filename_prefix}_{idx:02d}_{name.replace(' ', '_')}.png")
            fig.savefig(path, dpi=200, bbox_inches="tight", pad_inches=0.5)
        if show:
            plt.show()
        else:
            plt.close(fig)
    return figs



# ----------------------------
# Main
# ----------------------------

def main():
    p = argparse.ArgumentParser(description='Cable landing multi-algorithm analysis & plotting')
    p.add_argument('--ol-sync-dir', required=True, type=str)
    p.add_argument('--ol-async-dir', required=True, type=str)
    p.add_argument('--cl-sync-dir', required=True, type=str)
    p.add_argument('--cl-async-dir', required=True, type=str)
    p.add_argument('--output-dir', required=True, type=str)
    args = p.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    alg_dirs = [
        ('Open-loop sync', args.ol_sync_dir),
        ('Open-loop async', args.ol_async_dir),
        ('Closed-loop sync', args.cl_sync_dir),
        ('Closed-loop async', args.cl_async_dir),
    ]

    # Load
    loaded = []
    for name, d in alg_dirs:
        runs = load_runs(d)
        loaded.append((name, runs))
        
    # Plot
    plot_all_cable_landings_3d(loaded, show=False, save_dir=args.output_dir)

if __name__ == '__main__':
    main()
