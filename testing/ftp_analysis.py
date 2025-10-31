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
from iii_drone_interfaces.msg import StringStamped

# Your internal util; assumed available
from iii_drone_core.utils import math as iii_math  # for quatToMat

TOPIC_TYPE_MAP = {
    '/tf': TFMessage,
    '/control/trajectory_generator/trajectory_path': Path,
    '/control/trajectory_generator/target_pose': PoseStamped,
    '/fmu/out/vehicle_odometry': VehicleOdometry,
    '/mission/mission_executor/maneuver_reference_client/reference_mode': StringStamped,
}

# ----------------------------
# Helpers
# ----------------------------

def _quat_wxyz_to_rotmat(qwxyzw: np.ndarray) -> np.ndarray:
    """qwxyzw = [w,x,y,z] -> 3x3 rotation matrix."""
    return iii_math.quatToMat(qwxyzw)  # existing util

def _path_length(xyz: np.ndarray) -> float:
    if xyz.shape[0] < 2:
        return 0.0
    d = np.diff(xyz, axis=0)
    return float(np.sum(np.linalg.norm(d, axis=1)))

def _nearest_neighbor_distances(A: np.ndarray, B: np.ndarray, chunk: int = 2048) -> np.ndarray:
    """
    For each point in A (Nx3), compute distance to nearest point in B (Mx3).
    Pure NumPy (no SciPy). Chunked to limit memory.
    Returns distances (N,).
    """
    if B.shape[0] == 0 or A.shape[0] == 0:
        return np.zeros((A.shape[0],), dtype=float)
    out = np.empty((A.shape[0],), dtype=float)
    for i in range(0, A.shape[0], chunk):
        Ai = A[i:i+chunk]  # (k,3)
        # (k,1,3) - (1,m,3) -> (k,m,3) -> (k,m)
        diff = Ai[:, None, :] - B[None, :, :]
        dist2 = np.einsum('ijk,ijk->ij', diff, diff)
        out[i:i+chunk] = np.sqrt(dist2.min(axis=1))
    return out

def _align_vectors(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """
    Returns a 3x3 rotation matrix R that rotates unit vector u to unit vector v.
    Handles the parallel / anti-parallel edge cases.
    """
    u = u / (np.linalg.norm(u) + 1e-12)
    v = v / (np.linalg.norm(v) + 1e-12)
    c = np.dot(u, v)
    if c > 1.0: c = 1.0
    if c < -1.0: c = -1.0

    if np.isclose(c, 1.0, atol=1e-9):
        return np.eye(3)

    if np.isclose(c, -1.0, atol=1e-9):
        # 180 deg: rotate around any axis orthogonal to u
        # find a vector orthogonal to u
        a = np.array([1.0, 0.0, 0.0])
        if np.allclose(np.abs(u), np.array([1.0, 0.0, 0.0]), atol=1e-6):
            a = np.array([0.0, 1.0, 0.0])
        axis = np.cross(u, a)
        axis = axis / (np.linalg.norm(axis) + 1e-12)
        K = np.array([[0, -axis[2], axis[1]],
                       [axis[2], 0, -axis[0]],
                       [-axis[1], axis[0], 0]])
        # Rodrigues with theta=pi -> R = I + 2 K^2
        return np.eye(3) + 2 * (K @ K)

    # general case
    axis = np.cross(u, v)
    s = np.linalg.norm(axis)
    axis = axis / (s + 1e-12)
    K = np.array([[0, -axis[2], axis[1]],
                   [axis[2], 0, -axis[0]],
                   [-axis[1], axis[0], 0]])
    # Rodrigues' rotation: R = I + K*sinθ + K^2*(1-cosθ)
    R = np.eye(3) + K * s + (K @ K) * (1.0 - c)
    return R

def _apply_similarity_transform(xyz: np.ndarray, R: np.ndarray, s: float, t: np.ndarray) -> np.ndarray:
    # y = s * R * x + t
    return (s * (R @ xyz.T)).T + t

def _compute_cmd_based_similarity(start: np.ndarray, end: np.ndarray, target_end=np.array([3.0, 3.0, 3.0])):
    """
    Build a similarity transform so that:
      start -> (0,0,0)
      end   -> target_end (3,3,3)
    Steps:
      1) translate by -start (so start -> 0)
      2) rotate to align (end-start) direction with target_end direction
      3) uniform scale to match segment length to |target_end|
      4) final translation is zero (since we pinned start to origin)
    Returns (R, s, t, pre_translate)
      Apply as: y = s * R * (x - pre_translate) + t
    """
    v = end - start
    if np.linalg.norm(v) < 1e-9:
        # Degenerate: keep identity transform, just translate start to origin and set scale to 1
        R = np.eye(3)
        s = 1.0
        t = np.zeros(3)
        pre_translate = start
        return R, s, t, pre_translate

    target_dir = target_end
    R = _align_vectors(v, target_dir)
    s = np.linalg.norm(target_dir) / (np.linalg.norm(v) + 1e-12)
    t = np.zeros(3)           # keep start at origin
    pre_translate = start      # subtract this first
    return R, s, t, pre_translate

def _path_length(xyz: np.ndarray) -> float:
    if xyz.size < 6:  # < 2 pts
        return 0.0
    d = np.diff(xyz, axis=0)
    return float(np.sum(np.linalg.norm(d, axis=1)))

def _resample_by_arclength(xyz: np.ndarray, N: int = 200) -> np.ndarray:
    """Return N points sampled uniformly by arclength from a polyline."""
    if xyz.shape[0] == 0:
        return np.zeros((N, 3))
    if xyz.shape[0] == 1:
        return np.repeat(xyz, N, axis=0)
    seg = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    if s[-1] <= 1e-12:
        return np.repeat(xyz[:1], N, axis=0)
    u = np.linspace(0.0, s[-1], N)
    out = np.zeros((N, 3))
    for k in range(3):
        out[:, k] = np.interp(u, s, xyz[:, k])
    return out

def _segment_distances(P: np.ndarray, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Distance from each point P[i] to segment AB."""
    v = B - A
    vv = np.dot(v, v) + 1e-12
    t = ((P - A) @ v) / vv
    t = np.clip(t, 0.0, 1.0)[:, None]
    proj = A + t * v
    return np.linalg.norm(P - proj, axis=1)

def _rmse(d: np.ndarray) -> float:
    d = np.asarray(d)
    return float(np.sqrt(np.mean(d**2))) if d.size else float("nan")


# ----------------------------
# Data container
# ----------------------------

class FTPData:
    """
    Holds one flight/run segment starting when the trajectory_path first appears,
    until reference_mode becomes 'hover'.
    """
    def __init__(self, tf_list, traj_list, target_list, odom_list, refmode_list):
        self.tf_msgs = tf_list
        self.traj_msgs = traj_list
        self.target_msgs = target_list
        self.odom_msgs = odom_list
        self.refmode_msgs = refmode_list

        self.tf_xyz, self.tf_qwxyz, self.tf_t = self._tf_to_numpy(self.tf_msgs)
        self.cmd_xyz = self._path_to_numpy(self.traj_msgs)               # commanded reference point vs time
        self.tgt_xyz, self.tgt_t = self._pose_to_numpy(self.target_msgs)
        self.odom_xyz, self.odom_t = self._odom_to_numpy(self.odom_msgs)

        if not self._check_target_const(self.tgt_xyz):
            raise RuntimeError("Target Pose is not constant within the captured segment.")

    # def apply_cmd_based_similarity(self, target_end=np.array([3.0, 3.0, 3.0])):
    #     """
    #     Use the streaming commanded path endpoints to construct a similarity transform
    #     that maps the commanded start->end to 0->target_end, then apply the same transform
    #     to both commanded and executed paths (and target position for completeness).
    #     """
    #     if self.cmd_xyz.shape[0] < 2:
    #         return self  # not enough info; skip

    #     start = self.cmd_xyz[0]
    #     end   = self.cmd_xyz[-1]

    #     R, s, t, pre = _compute_cmd_based_similarity(start, end, target_end)

    #     def transform(X):
    #         if X is None or X.size == 0:
    #             return X
    #         X0 = X - pre  # translate start to origin
    #         return _apply_similarity_transform(X0, R, s, t)

    #     # mutate in place so downstream metrics & plotting use transformed data
    #     self.cmd_xyz  = transform(self.cmd_xyz)
    #     self.tf_xyz   = transform(self.tf_xyz)
    #     self.odom_xyz = transform(self.odom_xyz)  # optional, if you also show/measure from odom series
    #     self.tgt_xyz  = transform(self.tgt_xyz)   # for completeness; target will land near (3,3,3)

    #     return self


    @staticmethod
    def _check_target_const(tgt_xyz: np.ndarray) -> bool:
        if tgt_xyz.shape[0] <= 1:
            return True
        return np.allclose(tgt_xyz, tgt_xyz[0], atol=1e-6)

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

    def normalize_to_target(self, desired_target=np.array([3.0, 3.0, 3.0])):
        """
        Rotate all series into the *final* TF orientation frame and translate so that
        the target lands at desired_target.
        """
        if self.tf_qwxyz.shape[0] == 0:
            return self

        R = _quat_wxyz_to_rotmat(self.tf_qwxyz[-1])
        def rot(x): return (R.T @ x.T).T

        self.tf_xyz   = rot(self.tf_xyz)
        self.cmd_xyz  = rot(self.cmd_xyz)
        self.tgt_xyz  = rot(self.tgt_xyz)
        self.odom_xyz = rot(self.odom_xyz)

        tgt = self.tgt_xyz[0] if self.tgt_xyz.shape[0] else np.zeros(3)
        diff = tgt - desired_target

        self.tf_xyz   = self.tf_xyz - diff
        self.cmd_xyz  = self.cmd_xyz - diff
        self.tgt_xyz  = self.tgt_xyz - diff
        self.odom_xyz = self.odom_xyz - diff

        # Rebase times to segment start
        ts0 = min([arr[0] for arr in (self.tf_t, self.tgt_t, self.odom_t) if arr.size > 0] or [0.0])
        self.tf_t   = self.tf_t - ts0 if self.tf_t.size else self.tf_t
        self.tgt_t  = self.tgt_t - ts0 if self.tgt_t.size else self.tgt_t
        self.odom_t = self.odom_t - ts0 if self.odom_t.size else self.odom_t
        return self

    # ----- metrics -----

    def metrics(self) -> dict:
        exec_xyz = self.tf_xyz
        cmd_xyz = self.cmd_xyz
        # tgt = self.tgt_xyz[0] if self.tgt_xyz.shape[0] else np.zeros(3)
        tgt = np.array([3.0, 3.0, 3.0])  # assume normalized

        duration = (self.tf_t[-1] - self.tf_t[0]) if self.tf_t.size > 1 else 0.0
        exec_len = _path_length(exec_xyz)
        cmd_len  = _path_length(cmd_xyz)

        final_err = float(np.linalg.norm(exec_xyz[-1] - tgt)) if exec_xyz.shape[0] else np.nan
        min_err   = float(np.min(np.linalg.norm(exec_xyz - tgt, axis=1))) if exec_xyz.shape[0] else np.nan

        # Simple tracking error: nearest commanded reference point to each executed point
        if exec_xyz.shape[0] and cmd_xyz.shape[0]:
            d_nn = _nearest_neighbor_distances(exec_xyz, cmd_xyz)
            track_rmse = float(np.sqrt(np.mean(d_nn**2)))
            track_max  = float(np.max(d_nn))
        else:
            track_rmse = np.nan
            track_max = np.nan

        return dict(
            duration_s=duration,
            exec_length_m=exec_len,
            cmd_length_m=cmd_len,
            final_target_error_m=final_err,
            min_target_error_m=min_err,
            tracking_rmse_m=track_rmse,
            tracking_max_m=track_max,
        )

def apply_algorithm_similarity_transform(runs, target_end=np.array([3.0, 3.0, 3.0]), *, start_tol=1e-3):
    """
    Compute ONE similarity transform for an algorithm so that the MEAN final executed
    position across runs maps to 'target_end'. Assumes all runs already start ~ at (0,0,0).
    Applies the same (R, s, t) to ALL runs (exec, commanded, odom, target).

    Uses helpers: _align_vectors, _apply_similarity_transform
    """
    if not runs:
        return

    # Sanity: starts aligned at origin?
    # for i, r in enumerate(runs):
    #     if r.tf_xyz.size:
    #         if np.linalg.norm(r.tf_xyz[0]) > start_tol:
    #             raise ValueError(
    #                 f"Run {i} start not at origin (||start||={np.linalg.norm(r.tf_xyz[0]):.4g}). "
    #                 "Make sure you zero starts before calling this function."
    #             )
    
    for r in runs:
        if r.tf_xyz.size:
            shift = r.tf_xyz[0]
            r.tf_xyz  = r.tf_xyz  - shift
            r.cmd_xyz = r.cmd_xyz - shift
            r.odom_xyz= r.odom_xyz- shift
            r.tgt_xyz = r.tgt_xyz - shift

    # Mean final executed position (vector from origin)
    finals = []
    for r in runs:
        if r.tf_xyz.size:
            finals.append(r.tf_xyz[-1])
        if r.cmd_xyz.size:
            finals.append(r.cmd_xyz[-1])
    if not finals:
        return
    mean_final = np.mean(np.vstack(finals), axis=0)

    # Build similarity: origin->origin, mean_final->target_end
    if np.linalg.norm(mean_final) < 1e-12:
        R = np.eye(3)
        s = 1.0
    else:
        R = _align_vectors(mean_final, target_end)
        s = np.linalg.norm(target_end) / (np.linalg.norm(mean_final) + 1e-12)
    t = np.zeros(3)  # keep common start at origin

    # Apply to all runs & all relevant series
    def tx(X):
        if X is None or X.size == 0:
            return X
        return _apply_similarity_transform(X, R, s, t)

    for r in runs:
        r.tf_xyz   = tx(r.tf_xyz)
        r.cmd_xyz  = tx(r.cmd_xyz)
        r.odom_xyz = tx(r.odom_xyz)   # if you use it
        r.tgt_xyz  = tx(r.tgt_xyz)    # for completeness


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

def extract_segment(reader: rosbag2_py.SequentialReader) -> FTPData:
    tf_world_to_drone = []
    traj_path = []
    tg_target_pose = []
    vehicle_odometry = []
    reference_mode = []

    topic_map = {
        '/tf': tf_world_to_drone,
        '/control/trajectory_generator/trajectory_path': traj_path,
        '/control/trajectory_generator/target_pose': tg_target_pose,
        '/fmu/out/vehicle_odometry': vehicle_odometry,
        '/mission/mission_executor/maneuver_reference_client/reference_mode': reference_mode,
    }

    latest_tf = latest_tp = latest_odom = latest_ref = None
    started = False

    while reader.has_next():
        topic, serialized, _ = reader.read_next()
        if topic not in topic_map:
            continue
        msg = deserialize_message(serialized, TOPIC_TYPE_MAP[topic])

        if started:
            # stop when we hit hover
            if topic == '/mission/mission_executor/maneuver_reference_client/reference_mode' and msg.data == 'hover':
                break

            if topic == '/tf':
                tr = msg.transforms[0]
                if not (tr.child_frame_id == 'drone' and tr.header.frame_id == 'world'):
                    continue

            topic_map[topic].append(msg)

        else:
            if topic == '/control/trajectory_generator/trajectory_path':
                started = True
                traj_path.append(msg)
                if latest_tf is not None: tf_world_to_drone.append(latest_tf)
                if latest_tp is not None: tg_target_pose.append(latest_tp)
                if latest_odom is not None: vehicle_odometry.append(latest_odom)
                if latest_ref is not None: reference_mode.append(latest_ref)
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

    ftp = FTPData(tf_world_to_drone, traj_path, tg_target_pose, vehicle_odometry, reference_mode)
    return ftp.normalize_to_target(np.array([3.0, 3.0, 3.0]))

def load_runs(dir_path: str):
    return [extract_segment(r) for r in get_readers(dir_path)]

# ----------------------------
# Plotting
# ----------------------------

class _HandlerGradientStraight(HandlerBase):
    def __init__(self, colors, **kwargs):
        super().__init__(**kwargs)
        self.colors = colors

    def create_artists(self, legend, orig_handle, x0, y0, width, height, fontsize, trans):
        y = y0 + 0.5 * height
        n = max(1, len(self.colors))
        gap = width * 0.01
        segw = (width - gap * (n - 1)) / n
        arts = []
        for i, c in enumerate(self.colors):
            xs = x0 + i * (segw + gap)
            xe = xs + segw
            arts.append(Line2D([xs, xe], [y, y], transform=trans, color=c, lw=1.5))
        return arts

class _HandlerColorSegments(HandlerBase):
    """
    Draw a horizontal row of short colored line segments (palette swatch) in the legend.
    Use dashed=True for dashed segments.
    """
    def __init__(self, colors, gap_scale=0.08, lw=2.0, dashed=False, dash_pattern=(6, 3), alpha=1.0):
        super().__init__()
        self.colors = colors or ['C0']
        self.lw = lw
        self.dashed = dashed
        self.dash_pattern = dash_pattern
        self.alpha = alpha
        self.gap_scale = gap_scale

    def create_artists(self, legend, orig_handle, x0, y0, width, height, fontsize, trans):
        y = y0 + 0.5 * height
        n = max(1, len(self.colors))
        gap = width * self.gap_scale
        segw = (width - gap * (n - 1)) / n
        arts = []
        for i, c in enumerate(self.colors):
            xs = x0 + i * (segw + gap)
            xe = xs + segw
            ln = Line2D([xs, xe], [y, y], transform=trans, color=c,
                        lw=self.lw, alpha=self.alpha)
            if self.dashed:
                ln.set_linestyle((0, self.dash_pattern))
            arts.append(ln)
        return arts

def _set_3d_bounds(ax, runs, *, include_cmd=True, target_end=np.array([3.0, 3.0, 3.0]),
                   pad_frac=0.05, mode='tight'):
    """
    mode='tight' -> per-axis tight limits with small pad (least whitespace).
    mode='cube'  -> expand to a cube (like before), but with a precise pad.
    """
    clouds = []
    for r in runs:
        if r.tf_xyz.size:  clouds.append(r.tf_xyz)
        if include_cmd and r.cmd_xyz.size: clouds.append(r.cmd_xyz)
    # ensure start/target are considered
    clouds.append(np.array([[0.0, 0.0, 0.0], target_end], dtype=float))

    P = np.vstack(clouds)
    mn = P.min(axis=0)
    mx = P.max(axis=0)
    span = np.maximum(mx - mn, 1e-12)

    if mode == 'tight':
        lo = mn - pad_frac * span
        hi = mx + pad_frac * span
        ax.set_xlim(lo[0], hi[0]); ax.set_ylim(lo[1], hi[1]); ax.set_zlim(lo[2], hi[2])
        # Match box aspect to data spans (no extra whitespace) if available
        if hasattr(ax, "set_box_aspect"):
            ax.set_box_aspect((hi - lo))  # (dx, dy, dz)

    elif mode == 'cube':
        rng = span.max()
        center = 0.5 * (mn + mx)
        pad_abs = pad_frac * rng
        lo = center - rng / 2 - pad_abs
        hi = center + rng / 2 + pad_abs
        ax.set_xlim(lo[0], hi[0]); ax.set_ylim(lo[1], hi[1]); ax.set_zlim(lo[2], hi[2])
        if hasattr(ax, "set_box_aspect"):
            ax.set_box_aspect((1, 1, 1))  # true cube

    # Turn off any automatic margins some mpl versions add
    try:
        ax.margins(x=0, y=0, z=0)
    except TypeError:
        ax.margins(0)

def plot_algorithm(runs, title: str, save_path: str, show: bool = False,
                   include_cmd=True, run_offset=0, font_size: int | float | None = 16,
                   label_pad: float = 12, tick_pad: float = 6, include_legend=False):

    fig = plt.figure(figsize=(7.5, 6.5))#, constrained_layout=True)
    # fig.set_constrained_layout_pads(w_pad=0.02, h_pad=0.02, wspace=0.02, hspace=0.02)
    ax = fig.add_subplot(111, projection='3d')

    # Straight line from common start to common target
    straight = np.linspace([0.0, 0.0, 0.0], [3.0, 3.0, 3.0], 100)
    ax.plot(straight[:,0], straight[:,1], straight[:,2],
            linestyle='dashed', linewidth=1.25, color='black', label=None)

    # Colors per run (repeatable)
    base_colors = ['C0', 'C1', 'C2', 'C3', 'C4', 'C5']
    for i, run in enumerate(runs):
        c = base_colors[i % len(base_colors)]
        ax.plot(run.tf_xyz[:,0], run.tf_xyz[:,1], run.tf_xyz[:,2],
                label=None, linewidth=2.0, color=c)
        if include_cmd and run.cmd_xyz.shape[0]:
            ax.plot(run.cmd_xyz[:,0], run.cmd_xyz[:,1], run.cmd_xyz[:,2],
                    linestyle='dashed', linewidth=1.0, color=c, alpha=0.75, label=None)

    # Start and target markers
    ax.scatter(0.0, 0.0, 0.0, marker='o', s=40, color='black', edgecolor='k', label=None)
    ax.scatter(3.0, 3.0, 3.0, marker='x', s=80, color='red', linewidths=2.0, label=None)

    # Labels
    ax.set_xlabel('X [m]', labelpad=label_pad)
    ax.set_ylabel('Y [m]', labelpad=label_pad)
    ax.set_zlabel('Z [m]', labelpad=label_pad)

    # >>> Set font sizes if requested <<<
    if font_size is not None:
        ax.xaxis.label.set_size(font_size)
        ax.yaxis.label.set_size(font_size)
        ax.zaxis.label.set_size(font_size)

        # Tick label sizes
        ax.tick_params(axis='both', which='major', labelsize=font_size)
        ax.tick_params(axis='both', which='minor', labelsize=font_size)
        ax.zaxis.set_tick_params(which='major', labelsize=font_size)
        ax.zaxis.set_tick_params(which='minor', labelsize=font_size)

        # Offset text (e.g., ×1e3)
        ax.xaxis.get_offset_text().set_size(font_size)
        ax.yaxis.get_offset_text().set_size(font_size)
        ax.zaxis.get_offset_text().set_size(font_size)

    # --- Tight per-axis bounds (no outward rounding) ---
    pts = []
    for r in runs:
        if r.tf_xyz.size:                 pts.append(r.tf_xyz)
        if include_cmd and r.cmd_xyz.size: pts.append(r.cmd_xyz)
    # include start/target so guides are inside bounds
    pts.append(np.array([[0.0, 0.0, 0.0], [3.0, 3.0, 3.0]], dtype=float))

    P   = np.vstack(pts)
    mn  = P.min(axis=0)
    mx  = P.max(axis=0)
    span = np.maximum(mx - mn, 1e-12)

    pad_frac = 0.02  # tiny breathing room
    lo = mn - pad_frac * span
    hi = mx + pad_frac * span

    # set tight float limits (DO NOT round to ints)
    ax.set_xlim3d(lo[0], hi[0])
    ax.set_ylim3d(lo[1], hi[1])
    ax.set_zlim3d(lo[2], hi[2])

    # match the data aspect so mpl doesn't inflate a cube
    if hasattr(ax, "set_box_aspect"):
        ax.set_box_aspect(hi - lo)

    ax.set_autoscale_on(False)

    # --- Integer ticks INSIDE the limits only ---
    def _ticks_inside(lo, hi):
        a = int(np.ceil(lo))
        b = int(np.floor(hi))
        if b < a:
            return []  # no integers inside; skip ticks
        return list(range(a, b + 1))

    ax.set_xticks(_ticks_inside(lo[0], hi[0]))
    ax.set_yticks(_ticks_inside(lo[1], hi[1]))
    ax.set_zticks(_ticks_inside(lo[2], hi[2]))

    int_fmt = mticker.FormatStrFormatter('%d')
    ax.xaxis.set_major_formatter(int_fmt)
    ax.yaxis.set_major_formatter(int_fmt)
    ax.zaxis.set_major_formatter(int_fmt)

    ax.xaxis.set_minor_locator(mticker.NullLocator())
    ax.yaxis.set_minor_locator(mticker.NullLocator())
    ax.zaxis.set_minor_locator(mticker.NullLocator())


    # add space between tick labels and axes
    ax.tick_params(axis='both', which='major', pad=tick_pad)
    ax.tick_params(axis='both', which='minor', pad=tick_pad)  # harmless if no minors
    ax.zaxis.set_tick_params(which='major', pad=tick_pad)
    ax.zaxis.set_tick_params(which='minor', pad=tick_pad)

    # Legend (use chosen font size if provided)
    # leg = ax.legend(loc='upper left', fontsize=(font_size if font_size is not None else 'medium'))
    # Build run-number text, e.g., "runs 1, 2, 3"
    run_nums = [str(run_offset + i + 1) for i in range(len(runs))]
    run_list_str = ", ".join(run_nums) if run_nums else "—"

    # Colors actually used for runs in this figure
    run_colors = [f'C{i % 10}' for i in range(len(runs))]

    # Single-color proxies
    straight_handle = Line2D([0], [0], color='black', linestyle='dashed', lw=1.25, label='Straight path')
    start_handle    = Line2D([0], [0], marker='o', color='black', markerfacecolor='black',
                            linestyle='None', markersize=6, label='Start')
    target_handle   = Line2D([0], [0], marker='x', color='red', linestyle='None',
                            markersize=8, label='Target')

    # Dummy proxies that will be rendered by our custom handlers
    exec_proxy = Rectangle((0, 0), 1, 1)
    cmd_proxy  = Rectangle((0, 0), 1, 1)  # we’ll draw dashed swatches with a custom handler

    if include_legend:
        ax.legend(
            handles=[straight_handle, exec_proxy, cmd_proxy, start_handle, target_handle],
            labels=[ 'Straight path',
                    f'Flown paths',
                    f'Planned paths',
                    'Start', 'Target' ],
            handler_map={
                exec_proxy: _HandlerColorSegments(run_colors, gap_scale=0.02, lw=2.2, dashed=False, alpha=0.9),
                cmd_proxy:  _HandlerColorSegments(run_colors, gap_scale=0.08, lw=1.6, dashed=True,  dash_pattern=(8, 4), alpha=0.9),
            },
            fontsize=(font_size if font_size is not None else 'medium'),
            loc='upper left'
        )

    # fig.tight_layout()
    # fig.savefig(save_path, dpi=300, bbox_inches='tight')
    fig.savefig(save_path, dpi=300)#, bbox_inches='tight', pad_inches=0.08)
    if show:
        plt.show()
    plt.close(fig)


# ----------------------------
# Stats & CSV
# ----------------------------

def summarize_and_save(all_stats: list, csv_path: str):
    """
    Writes per-run rows and per-algorithm aggregates.
    Handles both current metrics and any legacy keys that may appear.
    """
    import csv, numpy as np

    # Canonical columns (current)
    base_cols = [
        'algorithm','run_idx','duration_s',
        'exec_length_m','cmd_length_m',
        'final_target_error_m',
        'straight_dev_rmse_m','straight_dev_max_m',
        'cmd_dev_rmse_m','cmd_dev_max_m',
        'straightness_ratio',
    ]
    # Legacy columns we’ll include if present in any row
    legacy_cols = ['min_target_error_m', 'tracking_rmse_m', 'tracking_max_m']
    present_legacy = [k for k in legacy_cols if any(k in r for r in all_stats)]

    fieldnames = base_cols + present_legacy

    def numeric_vals(rows, key):
        vals = []
        for r in rows:
            v = r.get(key, None)
            if isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v)):
                vals.append(float(v))
        return np.asarray(vals, dtype=float)

    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        # Per-run rows (only write known fields; blank otherwise)
        for row in all_stats:
            w.writerow({k: row.get(k, "") for k in fieldnames})

        # Aggregates
        algos = sorted(set(r.get('algorithm', '') for r in all_stats))
        w.writerow({})
        w.writerow({'algorithm': 'AGGREGATES (mean±std) - units in headers'})

        metric_cols = [k for k in fieldnames if k not in ('algorithm', 'run_idx')]
        for algo in algos:
            rows = [r for r in all_stats if r.get('algorithm', '') == algo]

            def mu_sigma(key):
                a = numeric_vals(rows, key)
                if a.size == 0:
                    return ""
                if a.size == 1:
                    return f"{a.mean():.3f}±0.000"
                return f"{a.mean():.3f}±{a.std(ddof=1):.3f}"

            agg_row = {'algorithm': algo, 'run_idx': f"n={len(rows)}"}
            for key in metric_cols:
                agg_row[key] = mu_sigma(key)
            w.writerow(agg_row)


def compute_run_metrics(run, target_end=np.array([3.0, 3.0, 3.0]), N_resample: int = 200):
    exec_xyz = run.tf_xyz
    cmd_xyz  = run.cmd_xyz
    A = np.zeros(3)
    B = target_end

    # lengths
    exec_len = _path_length(exec_xyz)
    cmd_len  = _path_length(cmd_xyz)

    # final error (exec → target)
    final_err = float(np.linalg.norm(exec_xyz[-1] - B)) if exec_xyz.size else float("nan")

    # deviation from straight line
    d_line = _segment_distances(exec_xyz, A, B) if exec_xyz.size else np.array([])
    straight_rmse = _rmse(d_line)
    straight_max  = float(np.max(d_line)) if d_line.size else float("nan")

    # deviation from planned (resampled by arclength)
    if exec_xyz.size and cmd_xyz.size:
        E = _resample_by_arclength(exec_xyz, N_resample)
        C = _resample_by_arclength(cmd_xyz,  N_resample)
        d_cmd = np.linalg.norm(E - C, axis=1)
        cmd_rmse = _rmse(d_cmd)
        cmd_max  = float(np.max(d_cmd))
    else:
        cmd_rmse = float("nan")
        cmd_max  = float("nan")

    # straightness ratio (>=1 ideally)
    straight_len = float(np.linalg.norm(B - A))
    straightness_ratio = exec_len / straight_len if straight_len > 0 else float("nan")

    # duration (if you want to keep it)
    duration = (run.tf_t[-1] - run.tf_t[0]) if getattr(run, "tf_t", np.array([])).size > 1 else 0.0

    return dict(
        duration_s=duration,
        exec_length_m=exec_len,
        cmd_length_m=cmd_len,
        final_target_error_m=final_err,
        straight_dev_rmse_m=straight_rmse,
        straight_dev_max_m=straight_max,
        cmd_dev_rmse_m=cmd_rmse,
        cmd_dev_max_m=cmd_max,
        straightness_ratio=straightness_ratio,
    )


# ----------------------------
# Main
# ----------------------------

def main():
    p = argparse.ArgumentParser(description='FTP multi-algorithm analysis & plotting')
    p.add_argument('--open-loop-mpc-dir', required=True, type=str)
    p.add_argument('--closed-loop-mpc-dir', required=True, type=str)
    p.add_argument('--quintic-interpolation-dir', required=True, type=str)
    p.add_argument('--results-output-dir', required=True, type=str)
    p.add_argument('--no-commanded', action='store_true', help="Do not plot commanded reference paths")
    p.add_argument('--no-transform', action='store_true', help="Do not apply command-based similarity transform")
    args = p.parse_args()
                   

    os.makedirs(args.results_output_dir, exist_ok=True)

    alg_dirs = [
        ('Open-loop MPC', args.open_loop_mpc_dir),
        ('Closed-loop MPC', args.closed_loop_mpc_dir),
        ('Quintic interpolation', args.quintic_interpolation_dir),
    ]

    # Load
    loaded = []
    for name, d in alg_dirs:
        runs = load_runs(d)
        if not args.no_transform:
            # for r in runs:
           #     r.apply_cmd_based_similarity(np.array([3.0, 3.0, 3.0]))
           apply_algorithm_similarity_transform(runs, target_end=np.array([3.0, 3.0, 3.0]))
        loaded.append((name, runs))

    # Plots
    run_offset = 0
    all_stats_rows = []
    first = True
    for name, runs in loaded:
        # per-run stats
        # for i, r in enumerate(runs):
        #     m = r.metrics()
        #     all_stats_rows.append(dict(algorithm=name, run_idx=i+1+run_offset, **m))

        # per-algorithm figure
        fig_name = f"{name.lower().replace(' ', '_')}.pdf"
        out_path = os.path.join(args.results_output_dir, fig_name)
        plot_algorithm(runs, name, out_path, show=False, include_cmd=(not args.no_commanded), run_offset=run_offset, include_legend=first)
        first = False
        run_offset += len(runs)
        
        # inside your per-algorithm loop
        for i, r in enumerate(runs):
            m = compute_run_metrics(r, target_end=np.array([3.0, 3.0, 3.0]))
            all_stats_rows.append(dict(algorithm=name, run_idx=i+1+run_offset, **m))

    # CSV
    csv_path = os.path.join(args.results_output_dir, 'results.csv')
    summarize_and_save(all_stats_rows, csv_path)

if __name__ == '__main__':
    main()
