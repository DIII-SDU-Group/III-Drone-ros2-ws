#!/usr/bin/env python3
"""
pl_merge_tf.py

Given:
  --pl_csv path/to/powerline_topic.csv
  --tf_csv path/to/tf_topic.csv

Produces an in-memory DataFrame that:
  - starts from the powerline CSV rows,
  - adds columns for the latest world->drone transform (<= row timestamp),
  - has a single normalized time column 't' (seconds since first powerline stamp),
  - removes all 'lines[...].header.*' fields but keeps everything else.

Usage:
  python pl_merge_tf.py --pl_csv /path/to/powerline.csv --tf_csv /path/to/tf.csv \
    --source_frame world --target_frame drone --out merged.csv
"""

from __future__ import annotations
import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# --------- Helpers ---------

TS_COLS = ["stamp_sec", "stamp_nsec", "stamp"]

# patterns for powerline columns we need to drop (headers only)
LINES_HEADER_PATTERN = re.compile(r"^lines\[\d+\]\.header\..*")

_LINE_WORLD_X = re.compile(r"^lines\[(\d+)\]\.world_position\.x$")

# patterns for TF arrays (tf2_msgs/TFMessage -> transforms[i].*)
TF_INDEX_RE = re.compile(r"^transforms\[(\d+)\]\.")
TF_FIELD_MAP = {
    # src -> (alias, dtype)
    "header.stamp.sec": ("tf_stamp_sec", "int"),
    "header.stamp.nanosec": ("tf_stamp_nsec", "int"),
    "header.frame_id": ("tf_frame_id", "str"),
    "child_frame_id": ("tf_child_frame_id", "str"),
    "transform.translation.x": ("tf_tx", "float"),
    "transform.translation.y": ("tf_ty", "float"),
    "transform.translation.z": ("tf_tz", "float"),
    "transform.rotation.x": ("tf_qx", "float"),
    "transform.rotation.y": ("tf_qy", "float"),
    "transform.rotation.z": ("tf_qz", "float"),
    "transform.rotation.w": ("tf_qw", "float"),
}

line_color_map = {
    0: "#9467bd",  # purple
    1: "#ff7f0e",  # orange
    2: "#2ca02c",  # green
    3: "#d62728",  # red
}

# ---- quaternion helpers ------------------------------------------------------

# ------------------ math helpers ------------------

def _quat_to_R(qx, qy, qz, qw) -> np.ndarray:
    """Quaternion (x,y,z,w) -> 3x3 rotation matrix. Returns identity on NaNs."""
    q = np.array([qx, qy, qz, qw], dtype=float)
    if not np.isfinite(q).all():
        return np.eye(3)
    n = np.linalg.norm(q)
    if n == 0:
        return np.eye(3)
    x, y, z, w = q / n
    xx, yy, zz = x*x, y*y, z*z
    xy, xz, yz = x*y, x*z, y*z
    wx, wy, wz = w*x, w*y, w*z
    return np.array([
        [1 - 2*(yy + zz),     2*(xy - wz),         2*(xz + wy)],
        [    2*(xy + wz),  1 - 2*(xx + zz),        2*(yz - wx)],
        [    2*(xz - wy),      2*(yz + wx),     1 - 2*(xx + yy)],
    ], dtype=float)

def _to_line_frame(R_wl: np.ndarray, p_w: np.ndarray, origin_w: np.ndarray) -> np.ndarray:
    """World -> Line frame coords: p_L = R_wl^T (p_w - origin_w)."""
    return (R_wl.T @ (p_w - origin_w).T).T  # (N,3)

def _quat_mul(q, r):
    # Hamilton product q ⊗ r  (both as [x,y,z,w])
    x1,y1,z1,w1 = q
    x2,y2,z2,w2 = r
    return np.array([
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
        w1*w2 - x1*x2 - y1*y2 - z1*z2
    ])

# ------------------ discovery helpers ------------------

_LINE_WX = re.compile(r"^lines\[(\d+)\]\.world_position\.x$")
def _line_indices(df: pd.DataFrame) -> list[int]:
    return sorted({int(m.group(1)) for c in df.columns if (m := _LINE_WX.match(c))})

def _get_line_cols(i: int) -> Tuple[str, str, str, str]:
    return (
        f"lines[{i}].world_position.x",
        f"lines[{i}].world_position.y",
        f"lines[{i}].world_position.z",
        f"lines[{i}].id",
    )

def _q_normalize(q: np.ndarray) -> np.ndarray:
    # q shape (..., 4) -> normalized
    n = np.linalg.norm(q, axis=-1, keepdims=True)
    n = np.where(n == 0, 1.0, n)
    return q / n

def _q_multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """
    Hamilton product (w,x,y,z order not used here; we use (x,y,z,w) consistently).
    Both inputs shape (..., 4) in (x,y,z,w). Returns (..., 4) (x,y,z,w).
    """
    x1, y1, z1, w1 = np.moveaxis(q1, -1, 0)
    x2, y2, z2, w2 = np.moveaxis(q2, -1, 0)
    x = w1*x2 + x1*w2 + y1*z2 - z1*y2
    y = w1*y2 - x1*z2 + y1*w2 + z1*x2
    z = w1*z2 + x1*y2 - y1*x2 + z1*w2
    w = w1*w2 - x1*x2 - y1*y2 - z1*z2
    return np.stack([x, y, z, w], axis=-1)

def _q_rotate_vec(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """
    Rotate vector(s) v by unit quaternion(s) q (both broadcastable).
    q: (...,4) in (x,y,z,w), v: (...,3) -> (...,3)
    """
    q = _q_normalize(q)
    # Convert to axis-angle formula via optimized vector math
    x, y, z, w = np.moveaxis(q, -1, 0)  # each (...,)
    # Compute t = 2 * cross(q_vec, v)
    qv = np.stack([x, y, z], axis=-1)
    t = 2.0 * np.cross(qv, v)
    # v' = v + w * t + cross(q_vec, t)
    v_rot = v + (w[..., None] * t) + np.cross(qv, t)
    return v_rot


def load_with_stamp(csv_path: Path) -> pd.DataFrame:
    """Load CSV and ensure float 'stamp' and Int64 stamp_sec/nsec exist."""
    df = pd.read_csv(csv_path)
    # require at least 'stamp'
    if "stamp" not in df.columns:
        # try to compose from sec/nsec
        if {"stamp_sec", "stamp_nsec"}.issubset(df.columns):
            sec = pd.to_numeric(df["stamp_sec"], errors="coerce")
            nsec = pd.to_numeric(df["stamp_nsec"], errors="coerce")
            df["stamp"] = sec + nsec * 1e-9
        else:
            raise ValueError(f"{csv_path} missing 'stamp' (and stamp_sec/nsec).")
    df["stamp"] = pd.to_numeric(df["stamp"], errors="coerce")
    # Fill convenience Int64 types if present
    if "stamp_sec" in df:
        df["stamp_sec"] = pd.to_numeric(df["stamp_sec"], errors="coerce").astype("Int64")
    if "stamp_nsec" in df:
        df["stamp_nsec"] = pd.to_numeric(df["stamp_nsec"], errors="coerce").astype("Int64")
    return df


def detect_tf_indices(columns: List[str]) -> List[int]:
    """Find all transforms[i] indices present in TF CSV header."""
    idxs = set()
    for c in columns:
        m = TF_INDEX_RE.match(c)
        if m:
            idxs.add(int(m.group(1)))
    return sorted(list(idxs))


def tf_long_from_wide(tf_df_wide: pd.DataFrame) -> pd.DataFrame:
    """
    Convert one /tf CSV (wide, with transforms[i].*) to a long dataframe with columns:
      stamp, tf_frame_id, tf_child_frame_id, tf_tx, tf_ty, tf_tz, tf_qx, tf_qy, tf_qz, tf_qw
    (uses message receive 'stamp' if header.stamp.* missing)
    """
    cols = tf_df_wide.columns.tolist()
    idxs = detect_tf_indices(cols)
    if not idxs:
        # Could already be a non-array TF? Return empty to avoid accidental merge.
        return pd.DataFrame(columns=[
            "stamp", "tf_frame_id", "tf_child_frame_id",
            "tf_tx", "tf_ty", "tf_tz", "tf_qx", "tf_qy", "tf_qz", "tf_qw"
        ])

    frames: List[pd.DataFrame] = []
    for i in idxs:
        # Build dict of alias -> series (or NA)
        data: Dict[str, pd.Series] = {"stamp": pd.to_numeric(tf_df_wide["stamp"], errors="coerce")}
        for src, (alias, dtype) in TF_FIELD_MAP.items():
            col = f"transforms[{i}].{src}"
            if col in tf_df_wide.columns:
                s = tf_df_wide[col]
            else:
                s = pd.Series(pd.NA, index=tf_df_wide.index)
            data[alias] = s

        small = pd.DataFrame(data)

        # Prefer header stamp if present to recompute a more precise stamp
        # (If absent, keep message 'stamp' as-is.)
        if small["tf_stamp_sec"].notna().any() and small["tf_stamp_nsec"].notna().any():
            sec = pd.to_numeric(small["tf_stamp_sec"], errors="coerce").fillna(0)
            nsec = pd.to_numeric(small["tf_stamp_nsec"], errors="coerce").fillna(0)
            small["stamp"] = sec + nsec * 1e-9

        # dtypes
        for f in ["tf_tx", "tf_ty", "tf_tz", "tf_qx", "tf_qy", "tf_qz", "tf_qw", "stamp"]:
            small[f] = pd.to_numeric(small[f], errors="coerce")
        for f in ["tf_frame_id", "tf_child_frame_id"]:
            small[f] = small[f].astype("string")

        frames.append(small)

    out = pd.concat(frames, axis=0, ignore_index=True)
    # drop rows without child_frame_id or frame_id
    out = out.dropna(subset=["tf_child_frame_id", "tf_frame_id"])
    # sort for asof
    out = out.sort_values("stamp").reset_index(drop=True)
    return out


def filter_tf_chain(tf_long: pd.DataFrame, source_frame: str, target_frame: str) -> pd.DataFrame:
    """Keep only transforms where frame_id == source and child_frame_id == target."""
    if tf_long.empty:
        return tf_long
    return tf_long[
        (tf_long["tf_frame_id"] == source_frame) &
        (tf_long["tf_child_frame_id"] == target_frame)
    ].copy()


def normalize_time(df_power: pd.DataFrame) -> pd.Series:
    """t = seconds since first powerline stamp."""
    t0 = float(df_power["stamp"].min())
    return df_power["stamp"] - t0


def drop_lines_header_columns(df_power: pd.DataFrame) -> pd.DataFrame:
    """Remove columns matching lines[...].header.*"""
    keep_cols = [c for c in df_power.columns if not LINES_HEADER_PATTERN.match(c)]
    return df_power.loc[:, keep_cols].copy()


def normalize_tf_translation_to_first(
    df,
    tx_col: str = "tf_tx",
    ty_col: str = "tf_ty",
    tz_col: str = "tf_tz",
    *,
    in_place: bool = True,
    store_origin: bool = False,
    origin_prefix: str = "tf_origin_",
):
    """
    Shift TF translations so the first non-null (tx,ty,tz) becomes (0,0,0).
    - Keeps orientation columns (tf_qx, tf_qy, tf_qz, tf_qw) unchanged.
    - If `in_place=False`, returns a *copy*; otherwise mutates `df`.
    - If `store_origin=True`, stores the original origin values in columns:
        tf_origin_tx, tf_origin_ty, tf_origin_tz (constant across rows).

    Assumes df is time-ordered (or at least that “first” row is what you want).
    """
    _df = df if in_place else df.copy()

    # find first row with all three translations present
    mask = _df[tx_col].notna() & _df[ty_col].notna() & _df[tz_col].notna()
    if not mask.any():
        # nothing to normalize
        return _df

    first_idx = _df.index[mask][0]
    ox = float(_df.at[first_idx, tx_col])
    oy = float(_df.at[first_idx, ty_col])
    oz = float(_df.at[first_idx, tz_col])

    if store_origin:
        _df[f"{origin_prefix}tx"] = ox
        _df[f"{origin_prefix}ty"] = oy
        _df[f"{origin_prefix}tz"] = oz

    # subtract origin from all rows (preserve NaNs)
    _df[tx_col] = _df[tx_col] - ox
    _df[ty_col] = _df[ty_col] - oy
    _df[tz_col] = _df[tz_col] - oz

    return _df

def transform_powerlines_to_world(df: pd.DataFrame) -> pd.DataFrame:
    """
    From a merged dataframe (has tf_* columns and lines[*].projected_position.*),
    produce a NEW dataframe where:
      - For every present line index i: add lines[i].world_position.{x,y,z}
        computed as: p_world = R_world_drone * p_drone + t_world_drone.
      - Also compute ONE world_line_orientation.{x,y,z,w} using the
        first available lines[i].pose.orientation and the TF quaternion:
          q_world_line = q_world_drone * q_drone_line
        (stored once, same for all rows since line orientation is shared).
    Keeps all existing columns; only appends new ones.

    Assumptions:
      - TF columns exist per row: tf_tx, tf_ty, tf_tz, tf_qx, tf_qy, tf_qz, tf_qw
      - projected positions in the message are expressed in the DRONE frame
      - TF (frame_id=world, child=drone) encodes pose of drone in world.

    Returns: a NEW DataFrame with added world position/orientation columns.
    """
    required_tf = ["tf_tx", "tf_ty", "tf_tz", "tf_qx", "tf_qy", "tf_qz", "tf_qw"]
    for c in required_tf:
        if c not in df.columns:
            raise ValueError(f"Missing required TF column '{c}' in dataframe")

    out = df.copy()

    # Build per-row transforms: translation and quaternion (x,y,z,w)
    t = out[["tf_tx", "tf_ty", "tf_tz"]].to_numpy(dtype=float, copy=True)  # (N,3)
    q = out[["tf_qx", "tf_qy", "tf_qz", "tf_qw"]].to_numpy(dtype=float, copy=True)  # (N,4)
    q = _q_normalize(q)

    # Detect all line indices present from column names
    line_idx_pattern = re.compile(r"^lines\[(\d+)\]\.pose\.position\.x$")
    line_idxs: List[int] = []
    for col in out.columns:
        m = line_idx_pattern.match(col)
        if m:
            line_idxs.append(int(m.group(1)))
    line_idxs = sorted(set(line_idxs))

    # For each line: transform pose.position.{x,y,z} -> world_position.{x,y,z}
    for i in line_idxs:
        cx, cy, cz = (f"lines[{i}].pose.position.x",
                      f"lines[{i}].pose.position.y",
                      f"lines[{i}].pose.position.z")
        if not all(c in out.columns for c in (cx, cy, cz)):
            continue  # skip incomplete sets

        p = out[[cx, cy, cz]].to_numpy(dtype=float, copy=True)  # (N,3)
        # rotate + translate
        p_rot = _q_rotate_vec(q, p)  # (N,3)
        p_w = p_rot + t

        out[f"lines[{i}].world_position.x"] = p_w[:, 0]
        out[f"lines[{i}].world_position.y"] = p_w[:, 1]
        out[f"lines[{i}].world_position.z"] = p_w[:, 2]

    # Compute ONE world_line_orientation from the first available line orientation
    # (they're identical across lines). If none present, skip.
    ori_cols = None
    for i in line_idxs:
        cols = [f"lines[{i}].pose.orientation.{k}" for k in ("x", "y", "z", "w")]
        if all(c in out.columns for c in cols):
            ori_cols = cols
            break

    if ori_cols is not None:
        q_line_drone = out[ori_cols].to_numpy(dtype=float, copy=True)  # (N,4)
        q_line_drone = _q_normalize(q_line_drone)
        # Compose: q_world_line = q_world_drone * q_drone_line
        q_world_line = _q_multiply(q, q_line_drone)  # (N,4)
        # Store once (same semantic for all lines) as separate columns
        out["world_line_orientation.x"] = q_world_line[:, 0]
        out["world_line_orientation.y"] = q_world_line[:, 1]
        out["world_line_orientation.z"] = q_world_line[:, 2]
        out["world_line_orientation.w"] = q_world_line[:, 3]

    return out

def cleanup_powerline_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove original per-line position and orientation columns 
    from the dataframe, keeping only the transformed world coords 
    and the single shared orientation.
    """
    drop_cols = [
        col for col in df.columns
        if any(
            kw in col
            for kw in [
                ".pose.position.",
                ".pose.orientation."
            ]
        )
    ]
    cleaned_df = df.drop(columns=drop_cols, errors="ignore")
    return cleaned_df

def add_drone_pose_and_drop_tf(df: pd.DataFrame) -> pd.DataFrame:
    """
    From world->drone TF columns (tf_tx, tf_ty, tf_tz, tf_qx, tf_qy, tf_qz, tf_qw),
    compute the drone pose in world coordinates (translation + quaternion).
    Adds new columns:
        drone_world_x, drone_world_y, drone_world_z,
        drone_world_qx, drone_world_qy, drone_world_qz, drone_world_qw
    Then removes the original tf_* columns.

    Assumes quaternions are in (x, y, z, w) order and encode rotation world->drone.
    """

    required = ["tf_tx", "tf_ty", "tf_tz", "tf_qx", "tf_qy", "tf_qz", "tf_qw"]
    for c in required:
        if c not in df.columns:
            raise ValueError(f"Missing required TF column: {c}")

    out = df.copy()

    out["drone_world_x"] = out["tf_tx"]
    out["drone_world_y"] = out["tf_ty"]
    out["drone_world_z"] = out["tf_tz"]
    out["drone_world_qx"] = out["tf_qx"]
    out["drone_world_qy"] = out["tf_qy"]
    out["drone_world_qz"] = out["tf_qz"]
    out["drone_world_qw"] = out["tf_qw"]

    return out

def drop_projected_positions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove all lines[*].projected_position.{x,y,z} columns from the dataframe.
    Keeps transformed world_position columns and everything else.
    """
    drop_cols = [c for c in df.columns if ".projected_position." in c]
    return df.drop(columns=drop_cols, errors="ignore")


# --------- Main operation ---------

def merge_powerline_with_tf(
    pl_csv: Path,
    tf_csv: Path,
    source_frame: str = "world",
    target_frame: str = "drone",
    tolerance_seconds: float | None = None,  # new: numeric tolerance (seconds)
) -> pd.DataFrame:
    # 1) load inputs
    df_power = load_with_stamp(pl_csv)
    df_tf_wide = load_with_stamp(tf_csv)

    # 2) make tf long (one row per transform[i]) and filter to source->target
    tf_long = tf_long_from_wide(df_tf_wide)
    tf_chain = filter_tf_chain(tf_long, source_frame, target_frame)

    # 3) prepare for asof join
    df_power["stamp"] = pd.to_numeric(df_power["stamp"], errors="coerce").astype("float64")
    tf_chain["stamp"]  = pd.to_numeric(tf_chain["stamp"],  errors="coerce").astype("float64")

    df_power = df_power.dropna(subset=["stamp"]).sort_values("stamp").reset_index(drop=True)
    tf_chain = tf_chain.dropna(subset=["stamp"]).sort_values("stamp").reset_index(drop=True)

    tf_use = tf_chain[[
        "stamp", "tf_frame_id", "tf_child_frame_id",
        "tf_tx", "tf_ty", "tf_tz", "tf_qx", "tf_qy", "tf_qz", "tf_qw"
    ]].copy()

    if tolerance_seconds is None:
        merged = pd.merge_asof(
            df_power,
            tf_use,
            on="stamp",
            direction="backward",
        )
    else:
        merged = pd.merge_asof(
            df_power,
            tf_use,
            on="stamp",
            direction="backward",
            tolerance=float(tolerance_seconds),  # must be float
        )

    # 4) add normalized single time column and drop header/timestamp clutter
    merged["t"] = normalize_time(merged)
    merged = drop_lines_header_columns(merged)

    for col in ["stamp", "stamp_sec", "stamp_nsec", "stamp.sec", "stamp.nanosec"]:
        if col in merged.columns:
            merged = merged.drop(columns=[col])

    merged = drop_projected_positions(merged)

    merged = normalize_tf_translation_to_first(merged)
    
    merged = transform_powerlines_to_world(merged)

    merged = cleanup_powerline_columns(merged)
    
    merged = add_drone_pose_and_drop_tf(merged)

    return merged

def get_line_indices_worldpos(df: pd.DataFrame) -> list[int]:
    return sorted({int(m.group(1)) for c in df.columns if (m := _LINE_WORLD_X.match(c))})

def get_line_id_for_index(df: pd.DataFrame, idx: int) -> int | None:
    col = f"lines[{idx}].id"
    if col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if not s.empty:
            return int(s.mode().iloc[0])
    return None

# --- compute fixed (average) cable positions over first N rows ---
def compute_cable_means_firstN(df: pd.DataFrame, N: int = 30) -> pd.DataFrame:
    """
    Returns a dataframe with columns: line_idx, line_id, x, y, z
    Averaged over the *first N rows* of df (after coercion).
    """
    if N is None or N <= 0:
        N = len(df)
    dfN = df.iloc[:min(N, len(df))]

    rows = []
    for i in get_line_indices_worldpos(dfN):
        cx = f"lines[{i}].world_position.x"
        cy = f"lines[{i}].world_position.y"
        cz = f"lines[{i}].world_position.z"
        if all(col in dfN.columns for col in (cx, cy, cz)):
            x = pd.to_numeric(dfN[cx], errors="coerce")
            y = pd.to_numeric(dfN[cy], errors="coerce")
            z = pd.to_numeric(dfN[cz], errors="coerce")
            rows.append({
                "line_idx": i,
                "line_id": get_line_id_for_index(dfN, i),
                "x": float(x.mean(skipna=True)),
                "y": float(y.mean(skipna=True)),
                "z": float(z.mean(skipna=True)),
            })
    return pd.DataFrame(rows)

# --- equal aspect for 3D ---
def _set_equal_3d(ax):
    """
    Make 3D axes have equal scale on x/y/z. Compatible with NumPy >= 2.0.
    Call AFTER plotting (once limits are known).
    """
    # First, try the modern way (Matplotlib >= 3.3/3.4)
    try:
        # This sets the physical box aspect to 1:1:1; often sufficient by itself
        ax.set_box_aspect((1, 1, 1))
        return
    except Exception:
        pass

    # Fallback: manually expand limits to the same half-range
    xlim = np.asarray(ax.get_xlim3d(), dtype=float)
    ylim = np.asarray(ax.get_ylim3d(), dtype=float)
    zlim = np.asarray(ax.get_zlim3d(), dtype=float)

    x_mid, y_mid, z_mid = np.mean(xlim), np.mean(ylim), np.mean(zlim)
    # use np.ptp instead of arr.ptp()
    x_range = np.ptp(xlim)
    y_range = np.ptp(ylim)
    z_range = np.ptp(zlim)

    # guard against zero/NaN ranges
    ranges = np.array([x_range, y_range, z_range], dtype=float)
    ranges[~np.isfinite(ranges)] = 0.0
    max_half_range = max(ranges.max() / 2.0, 1e-9)

    ax.set_xlim3d(x_mid - max_half_range, x_mid + max_half_range)
    ax.set_ylim3d(y_mid - max_half_range, y_mid + max_half_range)
    ax.set_zlim3d(z_mid - max_half_range, z_mid + max_half_range)

def estimate_corridor_and_fit_parabolas(
    df: pd.DataFrame,
    *,
    avg_n: int = 30,
    lateral_threshold: float = 2.0,   # meters; tune per dataset
    sustain: int = 10,                # consecutive samples beyond threshold
    origin_mode: str = "drone_firstN_mean",  # or "zero" or "line_firstN_mean"
) -> Tuple[Dict[str, int | float], pd.DataFrame, Dict[int, np.ndarray]]:
    """
    Returns:
      event: {'exit_index': int, 'exit_time': float}
      fits:  DataFrame with per-line rows:
             [line_idx, line_id, a, b, c, y_mean, s_min, s_max, n_used]
      samples: dict line_idx -> (M,3) sampled points in line frame [s, y(s), z(s)] for plotting

    Assumptions:
      - df has columns:
         * world_line_orientation.{x,y,z,w}  (single set; use the first valid row)
         * lines[i].world_position.{x,y,z}, lines[i].id
         * drone_world_{x,y,z}
      - The cable direction is the X-axis of the line frame (as per your spec).
    """
    # ---- 1) Rotation and origin for the line frame ----
    # Use first valid orientation row
    ori_cols = ["world_line_orientation.x","world_line_orientation.y",
                "world_line_orientation.z","world_line_orientation.w"]
    if not all(c in df.columns for c in ori_cols):
        raise ValueError("Missing world_line_orientation.{x,y,z,w} columns")
    first_valid = df.dropna(subset=ori_cols).head(1)
    if first_valid.empty:
        raise ValueError("No valid world_line_orientation row found")
    qx,qy,qz,qw = (float(first_valid[ori_cols[0]].iloc[0]),
                   float(first_valid[ori_cols[1]].iloc[0]),
                   float(first_valid[ori_cols[2]].iloc[0]),
                   float(first_valid[ori_cols[3]].iloc[0]))
    R_wl = _quat_to_R(qx,qy,qz,qw)   # maps Line->World; columns are line-frame axes in world

    # Choose origin in world to stabilize coordinates
    if origin_mode == "drone_firstN_mean":
        need = ["drone_world_x","drone_world_y","drone_world_z"]
        if not all(c in df.columns for c in need):
            raise ValueError("Missing drone_world_{x,y,z} columns")
        N = min(avg_n, len(df))
        origin_w = np.array([
            np.nanmean(pd.to_numeric(df["drone_world_x"].iloc[:N], errors="coerce")),
            np.nanmean(pd.to_numeric(df["drone_world_y"].iloc[:N], errors="coerce")),
            np.nanmean(pd.to_numeric(df["drone_world_z"].iloc[:N], errors="coerce")),
        ], dtype=float)
    elif origin_mode == "zero":
        origin_w = np.zeros(3, dtype=float)
    elif origin_mode == "line_firstN_mean":
        # mean of all line points first N rows
        N = min(avg_n, len(df))
        xs,ys,zs = [],[],[]
        for i in _line_indices(df):
            cx,cy,cz,_ = _get_line_cols(i)
            xs.append(pd.to_numeric(df[cx].iloc[:N], errors="coerce"))
            ys.append(pd.to_numeric(df[cy].iloc[:N], errors="coerce"))
            zs.append(pd.to_numeric(df[cz].iloc[:N], errors="coerce"))
        origin_w = np.array([
            np.nanmean(pd.concat(xs, axis=0)),
            np.nanmean(pd.concat(ys, axis=0)),
            np.nanmean(pd.concat(zs, axis=0)),
        ], dtype=float)
    else:
        raise ValueError(f"Unknown origin_mode: {origin_mode}")

    # ---- 2) Transform to line frame: lines and drone ----
    idxs = _line_indices(df)
    if not idxs:
        raise ValueError("No per-line world positions found")

    # Drone
    d_need = ["drone_world_x","drone_world_y","drone_world_z"]
    if not all(c in df.columns for c in d_need):
        raise ValueError("Missing drone_world_{x,y,z} columns")
    d_w = df[d_need].astype(float).to_numpy()
    d_L = _to_line_frame(R_wl, d_w, origin_w)      # columns: [s_d, y_d, z_d]

    # Lines (stack into per-line dict)
    lines_L: Dict[int, np.ndarray] = {}
    line_ids: Dict[int, Optional[int]] = {}
    for i in idxs:
        cx,cy,cz,cid = _get_line_cols(i)
        p_w = df[[cx,cy,cz]].astype(float).to_numpy()
        lines_L[i] = _to_line_frame(R_wl, p_w, origin_w)  # (N,3)
        if cid in df.columns:
            s_id = pd.to_numeric(df[cid], errors="coerce").dropna()
            line_ids[i] = int(s_id.mode().iloc[0]) if not s_id.empty else None
        else:
            line_ids[i] = None

    # ---- 3) Identify top cable (highest mean z over first avg_n) ----
    top_idx = None
    top_mean_z = -np.inf
    N0 = min(avg_n, len(df))
    for i, P in lines_L.items():
        m = np.nanmean(P[:N0, 2])  # z in line frame
        if m > top_mean_z:
            top_mean_z = m
            top_idx = i
    if top_idx is None:
        raise RuntimeError("Failed to identify top cable")

    # ---- 4) Detect corridor exit time (lateral distance to top cable) ----
    # Lateral distance in YZ-plane between drone and top cable at each time.
    # We compare the drone to the *nearest* top-cable sample in time (same row index).
    top_P = lines_L[top_idx]
    dy = d_L[:,1] - top_P[:,1]
    dz = d_L[:,2] - top_P[:,2]
    r = np.sqrt(dy*dy + dz*dz)

    # Debounced threshold crossing: first index where r>thr for 'sustain' consecutive samples
    thr = float(lateral_threshold)
    k_exit = None
    count = 0
    for k, rv in enumerate(r):
        if np.isfinite(rv) and rv > thr:
            count += 1
            if count >= sustain:
                k_exit = k - sustain + 1
                break
        else:
            count = 0
    if k_exit is None:
        k_exit = len(df) - 1  # never exits; consider whole run

    exit_time = float(df["t"].iloc[k_exit] if "t" in df.columns else k_exit)

    # ---- 5) Fit per-line parabolas z(s) within [0, k_exit] ----
    fits_rows = []
    samples: Dict[int, np.ndarray] = {}
    for i, P in lines_L.items():
        # Use only rows up to exit
        Pi = P[:k_exit+1, :]
        s = Pi[:,0]
        y = Pi[:,1]
        z = Pi[:,2]

        # Keep finite points
        mask = np.isfinite(s) & np.isfinite(z)
        if not np.any(mask):
            continue
        s_fit = s[mask]
        z_fit = z[mask]
        # Quadratic fit z(s) = a s^2 + b s + c
        # If too few points, degrade gracefully
        deg = 2 if len(s_fit) >= 3 else (1 if len(s_fit) >= 2 else 0)
        coeffs = np.polyfit(s_fit, z_fit, deg)  # returns [a,b,c] (or [m,b] / [c])

        # Ensure we always report (a,b,c)
        if deg == 2:
            a,b,c = coeffs
        elif deg == 1:
            m,b = coeffs
            a,b,c = 0.0, m, b
        else:
            c = coeffs[0]
            a,b = 0.0, 0.0

        # y is roughly constant; report its mean (finite)
        y_mean = float(np.nanmean(y[np.isfinite(y)]))

        # Usable s-range inside the window
        s_min = float(np.nanmin(s_fit))
        s_max = float(np.nanmax(s_fit))

        fits_rows.append({
            "line_idx": i,
            "line_id": line_ids.get(i),
            "a": float(a), "b": float(b), "c": float(c),
            "y_mean": y_mean,
            "s_min": s_min, "s_max": s_max,
            "n_used": int(len(s_fit)),
        })

        # Provide a small sampled curve in line frame (for quick plotting later)
        ss = np.linspace(s_min, s_max, num=100)
        zz = a*ss*ss + b*ss + c
        yy = np.full_like(ss, y_mean)
        samples[i] = np.stack([ss, yy, zz], axis=1)  # (100,3) in Line frame

    fits_df = pd.DataFrame(fits_rows).sort_values("line_idx").reset_index(drop=True)

    event = {"exit_index": int(k_exit), "exit_time": exit_time}
    return event, fits_df, samples

# --- main plot ---
def plot_3d_overview_cables_and_drone_firstN(
    df: pd.DataFrame,
    avg_n: int = 30,
    drone_path_columns=("drone_world_x", "drone_world_y", "drone_world_z"),
    save_path: str | None = None,
    show: bool = True,
):
    """
    - df: preprocessed dataframe with lines[i].world_position.{x,y,z} and lines[i].id,
          and drone_world_{x,y,z}
    - avg_n: number of initial rows to average for fixed cable positions (default 30)
    - color_map: fixed mapping {0..3: color}; if None, uses make_fixed_color_map()
    """
    means = compute_cable_means_firstN(df, N=avg_n)
    if means.empty:
        raise ValueError("No per-line world positions found.")

    # fixed color dict for ids 0..3
    color_map = line_color_map

    # drone path
    dx, dy, dz = drone_path_columns
    if not all(c in df.columns for c in (dx, dy, dz)):
        raise ValueError(f"Missing drone path columns: {drone_path_columns}")
    drone_xyz = df[[dx, dy, dz]].dropna().to_numpy(dtype=float)

    fig = plt.figure()
    ax = fig.add_subplot(111, projection="3d")

    # Drone path as line
    ax.plot(drone_xyz[:,0], drone_xyz[:,1], drone_xyz[:,2], linewidth=1.5, label="Drone path")

    # Cable means as dots (use id 0..3 for color; fallback to line_idx if id missing)
    for i, row in enumerate(means.itertuples()):
        c = color_map.get(i, "#7f7f7f")  # grey fallback if id not in {0..3}
        ax.scatter(row.x, row.y, row.z, s=150, color=c, label=f"Cable {i}")

    # De-duplicate legend
    h, l = ax.get_legend_handles_labels()
    uniq = dict(zip(l, h))
    ax.legend(uniq.values(), uniq.keys(), loc="upper right")

    ax.set_xlabel("X [m]")
    ax.set_ylabel("Y [m]")
    ax.set_zlabel("Z [m]")

    _set_equal_3d(ax)

    if save_path:
        fig.savefig(save_path)
    if show:
        plt.show()
    plt.close(fig)
    

def _to_world(R_wl: np.ndarray, p_L: np.ndarray, origin_w: np.ndarray) -> np.ndarray:
    # Line -> World: p_w = R_wl @ p_L + origin_w
    return (R_wl @ p_L.T).T + origin_w
    
def plot_visibility_timeseries(df, reentry_jumps, line_color_map, jitter=0.05,
                               save_path=None, show=True):
    """
    Plot binary visibility vs time for all cables (indices 0–3).
    """

    fig, ax = plt.subplots(figsize=(10, 4), constrained_layout=True)

    t = df['t'].values

    multiplier = 1.2

    for i in range(4):
        col = f'lines[{i}].in_field_of_view'
        if col not in df.columns:
            continue
        vis = df[col].astype(int).values
        offset = (i - 1.5) * jitter  # distribute around 0
        ax.step(t, vis + offset,
                where='post',
                color=line_color_map.get(i, 'k'),
                label=f'Cable {i}')

        # try:
        #     reentry_jumps: pd.DataFrame
        #     t_enter = reentry_jumps.loc[i, 't_enter']
        #     ax.axvline(t_enter, color='gray', linestyle='--', alpha=0.5)
        #     ax.text(
        #         t_enter, 1.05, 'Re-entry',
        #         rotation=90,
        #         verticalalignment='bottom',
        #         horizontalalignment='right',
        #         color='gray',
        #         fontsize=12*multiplier   # manual fontsize
        #     )
        # except Exception:
        #     pass
        
    # axis labels
    ax.set_xlabel('Time [s]', fontsize=14*multiplier)
    # ax.set_ylabel('Visibility', fontsize=14)

    # y-ticks
    ax.set_yticks([0, 1])
    # ax.set_yticklabels(['Outside FOV', 'Inside FOV'], fontsize=12*multiplier)
    ax.set_yticklabels(
        ['Outside FOV', 'Inside FOV'],
        fontsize=12*multiplier,
        rotation=45,
        ha='right'   # aligns labels so they don’t overlap
    )

    # x-ticks
    ax.tick_params(axis='x', labelsize=12*multiplier)

    # legend
    ax.legend(loc='center right', ncol=2, fontsize=12*multiplier)

    if save_path:
        fig.savefig(save_path)

    if show:
        plt.show()

    plt.close()


def plot_cable_tracks_in_zy(df, line_color_map, downsample=1, save_path=None, show=True):
    """
    Plot all cable tracks in the powerline cross-section (YZ plane of the line frame).
    Color is vivid when the cable is in FOV, faded when out of FOV.

    Assumptions:
      - df has columns:
        'lines[i].world_position.{x,y,z}', 'lines[i].in_field_of_view' for i=0..3
        'world_line_orientation.{x,y,z,w}' = quaternion of the *powerline frame orientation in world*
      - If the powerline orientation varies over time, we use the per-row quaternion.

    Parameters
    ----------
    df : pandas.DataFrame
    line_color_map : dict  {index:int -> color}
    ax : matplotlib.axes.Axes or None
    downsample : int  plot every Nth sample (for large bags)

    Returns
    -------
    ax : matplotlib.axes.Axes
    """
    fig, ax = plt.subplots(figsize=(6, 6))

    # Extract quaternions (per-row) and precompute rotation matrices W_R_P (world_from_powerline)
    qx = df['world_line_orientation.x'].values[::downsample]
    qy = df['world_line_orientation.y'].values[::downsample]
    qz = df['world_line_orientation.z'].values[::downsample]
    qw = df['world_line_orientation.w'].values[::downsample]

    # For transforming world->powerline frame, we need P_R_W = (W_R_P)^T
    R_list = [ _quat_to_R(qx[i], qy[i], qz[i], qw[i]).T for i in range(len(qw)) ]

    def project_world_to_lineYZ(px, py, pz):
        """
        Given arrays of world positions (same length as R_list),
        return arrays (y_line, z_line) in the powerline frame (drop x_line).
        """
        y_line = np.empty_like(px, dtype=float)
        z_line = np.empty_like(px, dtype=float)
        for k in range(len(px)):
            Pw = np.array([px[k], py[k], pz[k]], dtype=float)
            Pl = R_list[k] @ Pw  # world -> line frame
            y_line[k] = Pl[1]
            z_line[k] = Pl[2]
        return y_line, z_line

    # Helper to plot with alpha depending on visibility (masking to keep lines continuous)
    def plot_vis_masked(x, y, vis_mask, color, label=None):
        # In-FOV segment
        x_in  = x.copy();  y_in  = y.copy()
        x_out = x.copy();  y_out = y.copy()
        # Mask the opposite states
        x_in[~vis_mask]  = np.nan;  y_in[~vis_mask]  = np.nan
        x_out[vis_mask]  = np.nan;  y_out[vis_mask]  = np.nan
        # Plot
        ax.plot(x_in,  y_in,  color=color, alpha=1.0,  linewidth=1.2, label=label)
        ax.plot(x_out, y_out, color=color, alpha=0.25, linewidth=1.2)

    # For each cable, project and plot
    for i in range(4):
        base = f'lines[{i}]'
        posx_col = f'{base}.world_position.x'
        if posx_col not in df.columns:
            continue

        px = df[posx_col].values[::downsample]
        py = df[f'{base}.world_position.y'].values[::downsample]
        pz = df[f'{base}.world_position.z'].values[::downsample]
        vis = df[f'{base}.in_field_of_view'].astype(bool).values[::downsample]

        y_line, z_line = project_world_to_lineYZ(px, py, pz)

        plot_vis_masked(y_line, z_line, vis, color=line_color_map.get(i, 'k'),
                        label=f'Cable {i+1}')

    # Cosmetics
    ax.set_aspect('equal', adjustable='box')
    ax.set_xlabel('Line-frame Y (m)  [cross-track]')
    ax.set_ylabel('Line-frame Z (m)  [vertical]')
    # ax.set_title('Cable estimates in powerline cross-section (YZ)')
    ax.grid(True, linestyle='--', alpha=0.4)
    ax.legend(loc='best', fontsize=9)

    if save_path:
        fig.savefig(save_path)
    if show:
        plt.show()
    plt.close()
    
def _set_equal_3d(ax):
    """Best-effort equal aspect for 3D axes."""
    try:
        ax.set_box_aspect((1,1,1))
        return
    except Exception:
        pass
    xlim = np.asarray(ax.get_xlim3d(), dtype=float)
    ylim = np.asarray(ax.get_ylim3d(), dtype=float)
    zlim = np.asarray(ax.get_zlim3d(), dtype=float)
    x_mid, y_mid, z_mid = np.mean(xlim), np.mean(ylim), np.mean(zlim)
    ranges = np.array([np.ptp(xlim), np.ptp(ylim), np.ptp(zlim)], dtype=float)
    max_half = max(ranges.max()/2.0, 1e-9)
    ax.set_xlim3d(x_mid - max_half, x_mid + max_half)
    ax.set_ylim3d(y_mid - max_half, y_mid + max_half)
    ax.set_zlim3d(z_mid - max_half, z_mid + max_half)

def plot_cable_tracks_3d_raw(df,
                             line_color_map,
                             ax=None,
                             downsample=1,
                             mode='line',           # 'line' or 'scatter'
                             alpha_in=1.0,
                             alpha_out=0.25,
                             plot_drone=True,
                             save_path=None,
                             show=True):
    """
    Plot *raw* world positions of cables in 3D (no projection), colored by visibility.

    Expects columns per cable i:
      'lines[i].world_position.x/y/z', 'lines[i].in_field_of_view' (bool/int)
    Optional (for drone path): 'drone_world_x/y/z'

    Parameters
    ----------
    df : pandas.DataFrame
    line_color_map : dict  {i -> color}
    ax : 3D axes or None
    downsample : int
    mode : 'line' (masked line) or 'scatter'
    alpha_in : float, opacity while in FOV
    alpha_out: float, opacity while out of FOV
    plot_drone : bool, plot drone trajectory if columns are present

    Returns
    -------
    ax : 3D axes
    """
    if ax is None:
        fig = plt.figure(figsize=(7,6))
        ax = fig.add_subplot(111, projection='3d')

    # Optional drone path (helps orient you)
    if plot_drone and all(c in df.columns for c in ('drone_world_x','drone_world_y','drone_world_z')):
        d = df[['drone_world_x','drone_world_y','drone_world_z']].to_numpy(dtype=float)[::downsample]
        ax.plot(d[:,0], d[:,1], d[:,2], lw=1.2, color='#1f77b4', alpha=0.6, label='Drone path')

    # Helper to plot with visibility masking
    def plot_vis_series(x, y, z, vis_mask, color, label=None):
        if mode == 'line':
            # mask opposite states with NaNs to preserve continuity
            xin, yin, zin = x.copy(), y.copy(), z.copy()
            xout, yout, zout = x.copy(), y.copy(), z.copy()
            xin[~vis_mask] = np.nan; yin[~vis_mask] = np.nan; zin[~vis_mask] = np.nan
            xout[vis_mask] = np.nan; yout[vis_mask] = np.nan; zout[vis_mask] = np.nan
            ax.plot(xin,  yin,  zin,  color=color, alpha=alpha_in,  lw=1.2, label=label)
            ax.plot(xout, yout, zout, color=color, alpha=alpha_out, lw=1.2)
        elif mode == 'scatter':
            ax.scatter(x[vis_mask], y[vis_mask], z[vis_mask], s=6, color=color, alpha=alpha_in, label=label)
            ax.scatter(x[~vis_mask], y[~vis_mask], z[~vis_mask], s=6, color=color, alpha=alpha_out)

    # Plot each cable
    for i in range(4):
        base = f'lines[{i}]'
        cols = [f'{base}.world_position.x', f'{base}.world_position.y', f'{base}.world_position.z']
        if not all(c in df.columns for c in cols):
            continue

        xyz = df[cols].to_numpy(dtype=float)[::downsample]
        vis = df.get(f'{base}.in_field_of_view', pd.Series(False, index=df.index)).astype(bool).values[::downsample]
        plot_vis_series(xyz[:,0], xyz[:,1], xyz[:,2], vis, color=line_color_map.get(i, 'k'), label=f'Cable {i+1}')

    # Cosmetics
    ax.set_xlabel('X [m]')
    ax.set_ylabel('Y [m]')
    ax.set_zlabel('Z [m]')
    _set_equal_3d(ax)
    # deduplicate legend entries
    h, l = ax.get_legend_handles_labels()
    if h:
        uniq = dict(zip(l, h))
        ax.legend(uniq.values(), uniq.keys(), loc='best', fontsize=9)
    ax.grid(True)

    if save_path:
        fig.savefig(save_path)
    if show:
        plt.show()
    plt.close()











def _normalize_quat_rows(q):
    n = np.linalg.norm(q, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return q / n

def _average_quat_firstN(df, N=50):
    Q = df[["world_line_orientation.x","world_line_orientation.y",
            "world_line_orientation.z","world_line_orientation.w"]].dropna().to_numpy(float)
    if len(Q) == 0:
        raise ValueError("No world_line_orientation in df.")
    Q = _normalize_quat_rows(Q[:min(N, len(Q))])
    C = (Q.T @ Q)
    w, V = np.linalg.eigh(C)
    q_mean = V[:, np.argmax(w)]
    if np.dot(q_mean, Q[0]) < 0:  # consistent sign
        q_mean = -q_mean
    return q_mean  # (x,y,z,w)

def _compute_lineframe_series(df, *, avg_quat_N=50, origin="drone", downsample=1):
    """
    Returns per-cable dict with arrays in a fixed line frame (YZ plane):
      {i: {'t','y','z','r','vis'}}
    r = sqrt(y^2 + z^2) is distance in the cross-section from the drone.
    """
    # fixed line frame
    qx, qy, qz, qw = _average_quat_firstN(df, N=avg_quat_N)
    R_wl = _quat_to_R(qx, qy, qz, qw)   # world_from_line
    R_lw = R_wl.T                       # line_from_world

    # origin per row: drone or plane point
    if origin == "drone":
        ox = df["drone_world_x"].values[::downsample]
        oy = df["drone_world_y"].values[::downsample]
        oz = df["drone_world_z"].values[::downsample]
    elif origin == "projection_plane":
        ox = df["projection_plane.point.x"].values[::downsample]
        oy = df["projection_plane.point.y"].values[::downsample]
        oz = df["projection_plane.point.z"].values[::downsample]
    else:
        raise ValueError("origin must be 'drone' or 'projection_plane'")
    O = np.stack([ox, oy, oz], axis=1)

    per = {}
    for i in range(4):
        cols = [f"lines[{i}].world_position.x",
                f"lines[{i}].world_position.y",
                f"lines[{i}].world_position.z"]
        if not all(c in df.columns for c in cols):
            continue
        Pw = df[cols].to_numpy(float)[::downsample]
        dPw = Pw - O
        Pl = (R_lw @ dPw.T).T
        y, z = Pl[:,1], Pl[:,2]
        vis = df.get(f"lines[{i}].in_field_of_view", pd.Series(False, index=df.index)).astype(bool).values[::downsample]
        t = df["t"].values[::downsample] if "t" in df.columns else np.arange(len(y), dtype=float)
        r = np.sqrt(y*y + z*z)
        per[i] = {"t":t, "y":y, "z":z, "r":r, "vis":vis}
    return per

# ---------- 1) variance vs distance (per cable) ----------
def plot_variance_vs_distance(df, line_color_map, *, nbins=12, avg_quat_N=50,
                              origin="drone", use_in_fov_mean=True, downsample=1,
                              save_path=None, show=True):
    """
    For each cable i:
      - compute per-bin 2D variance around the cable's reference point (mu_y, mu_z).
      - x-axis: distance from drone r (in YZ plane). y-axis: variance (m^2).
    """
    per = _compute_lineframe_series_perrow(df, avg_quat_N=avg_quat_N, origin=origin, downsample=downsample)
    fig, ax = plt.subplots(figsize=(7,5))

    for i, D in per.items():
        mask_ref = D["vis"] if use_in_fov_mean and np.any(D["vis"]) else np.isfinite(D["y"])
        mu_y = np.nanmean(D["y"][mask_ref])
        mu_z = np.nanmean(D["z"][mask_ref])

        e2 = (D["y"] - mu_y)**2 + (D["z"] - mu_z)**2
        r = D["r"]
        good = np.isfinite(e2) & np.isfinite(r)

        if good.sum() < 5:
            continue

        bins = np.linspace(np.nanmin(r[good]), np.nanmax(r[good]), nbins+1)
        idx = np.digitize(r, bins) - 1
        centers = 0.5*(bins[:-1] + bins[1:])
        var2d = np.full(nbins, np.nan)
        count = np.zeros(nbins, dtype=int)
        for b in range(nbins):
            m = good & (idx == b)
            if m.sum() >= 3:
                var2d[b] = np.nanmean(e2[m])  # "2D variance" (MSE)
                count[b] = m.sum()

        ax.scatter(centers, var2d, label=f"Cable {i+1}",
                color=line_color_map.get(i,'k'))

    ax.set_xlabel("Distance from drone r (m) in cross-section")
    ax.set_ylabel("Position estimate variance (m²)")
    ax.grid(True, linestyle='--', alpha=0.5)
    ax.legend()
    
    if save_path:
        fig.savefig(save_path)
    if show:
        plt.show()
    plt.close()

# ---------- 2) variance inside vs outside FOV ----------
def plot_variance_in_vs_out(df, line_color_map, *, avg_quat_N=50, origin="drone", downsample=1, save_path=None, show=True):
    """
    Bar plot per cable: variance (MSE in YZ) when visible vs not visible.
    """
    per = _compute_lineframe_series_perrow(df, downsample=downsample)
    data = []
    for i, D in per.items():
        # reference = mean when visible if available, else overall
        ref_mask = D["vis"] if np.any(D["vis"]) else np.isfinite(D["y"])
        mu_y = np.nanmean(D["y"][ref_mask]); mu_z = np.nanmean(D["z"][ref_mask])

        e2 = (D["y"] - mu_y)**2 + (D["z"] - mu_z)**2
        m_in  = D["vis"] & np.isfinite(e2)
        m_out = (~D["vis"]) & np.isfinite(e2)
        vin = np.nanmean(e2[m_in]) if m_in.any() else np.nan
        vout= np.nanmean(e2[m_out]) if m_out.any() else np.nan
        data.append((i, vin, vout))

    # plot
    fig, ax = plt.subplots(figsize=(7,4))
    x = np.arange(len(data))
    w = 0.38
    vin  = [d[1] for d in data]
    vout = [d[2] for d in data]
    ax.bar(x - w/2, vin,  width=w, label="In FOV")
    ax.bar(x + w/2, vout, width=w, label="Out of FOV")
    ax.set_xticks(x); ax.set_xticklabels([f"Cable {d[0]}" for d in data])
    ax.set_ylabel("Position estimate variance (m²)")
    ax.grid(True, axis='y', linestyle='--', alpha=0.5)
    ax.legend()
    if save_path:
        fig.savefig(save_path)
    if show:
        plt.show()
    plt.close()

# ---------- 3) robust detection of FOV re-entry & pre/post estimates ----------
def _compute_lineframe_series_perrow(df, *, downsample=1):
    """
    Project with the *same method as the working 2D plot*:
      - per-row line orientation
      - NO origin subtraction
      - Y,Z come from Pl = R(q)^T @ P_world  (then drop x)

    Returns per-cable dict:
      {i: {'t','y','z','r','vis'}}
    """
    # per-row rotations: world->line = R(q)^T
    qx = df['world_line_orientation.x'].values[::downsample]
    qy = df['world_line_orientation.y'].values[::downsample]
    qz = df['world_line_orientation.z'].values[::downsample]
    qw = df['world_line_orientation.w'].values[::downsample]
    R_list = [_quat_to_R(qx[k], qy[k], qz[k], qw[k]).T for k in range(len(qw))]

    # helper for one cable
    def project_world_to_lineYZ(px, py, pz):
        y_line = np.empty_like(px, dtype=float)
        z_line = np.empty_like(px, dtype=float)
        for k in range(len(px)):
            Pw = np.array([px[k], py[k], pz[k]], dtype=float)
            Pl = R_list[k] @ Pw  # world -> line frame (per-row)
            y_line[k] = Pl[1]
            z_line[k] = Pl[2]
        return y_line, z_line

    per = {}
    t = df["t"].values[::downsample] if "t" in df.columns else np.arange(len(R_list), dtype=float)

    for i in range(4):
        base = f'lines[{i}]'
        cx = f'{base}.world_position.x'
        cy = f'{base}.world_position.y'
        cz = f'{base}.world_position.z'
        if cx not in df.columns:  # cable missing
            continue

        px = df[cx].values[::downsample]
        py = df[cy].values[::downsample]
        pz = df[cz].values[::downsample]
        y, z = project_world_to_lineYZ(px, py, pz)

        vis = df.get(f'{base}.in_field_of_view', pd.Series(False, index=df.index)).astype(bool).values[::downsample]
        r = np.sqrt(y*y + z*z)  # distance in the YZ cross-section (no origin subtraction)

        per[i] = {"t": t, "y": y, "z": z, "r": r, "vis": vis}

    return per

def _rle_bool(x: np.ndarray):
    """Run-length encode boolean array -> values, lengths, starts."""
    if len(x) == 0:
        return np.array([], dtype=bool), np.array([], dtype=int), np.array([], dtype=int)
    x_int = x.astype(int)
    # indices where value changes
    change = np.flatnonzero(np.diff(x_int)) + 1
    starts = np.r_[0, change]
    ends   = np.r_[change, len(x)]
    lengths = ends - starts
    values = x[starts]
    return values, lengths, starts

def _debounce_vis(vis: np.ndarray, *, min_true_len: int, min_false_len: int) -> np.ndarray:
    """
    Flip short TRUE bursts to FALSE and short FALSE gaps to TRUE.
    This is a simple morphological open/close on boolean runs.
    """
    vals, lens, starts = _rle_bool(vis)
    out = vis.copy()
    for v, L, s in zip(vals, lens, starts):
        if v and L < min_true_len:
            out[s:s+L] = False
        if (not v) and L < min_false_len:
            out[s:s+L] = True
    return out

def detect_reentry_jumps(
        df,
        *,
        downsample=1,
        min_true_len=20,      # discard visible bursts shorter than this
        min_false_len=25,     # fill invisible gaps shorter than this
        pre_window=1,        # samples to use for "before" median (taken from the preceding FALSE run)
        post_window=30,       # samples to use for "after" median (taken from the start of the TRUE run),
        point_estimation_type="single",  # "mean", "weighted_mean", "single"
    ):
    """
    Re-entry detection that groups flicker into a single event per debounced TRUE run.
    Uses the SAME projection as your 2D plot via _compute_lineframe_series_perrow(df).

    Returns DataFrame:
      [line_idx, event_idx, t_enter, y_before, z_before, y_after, z_after, dy, dz, jump_norm]
    """
    per = _compute_lineframe_series_perrow(df, downsample=downsample)
    events = []

    for i, D in per.items():
        t = D["t"]; y = D["y"]; z = D["z"]
        vis_raw = D["vis"]
        if len(vis_raw) == 0:
            continue

        # 1) debounce
        vis = _debounce_vis(vis_raw, min_true_len=min_true_len, min_false_len=min_false_len)
        
        # print(f"Cable {i}:")
        # prev_v = None
        # for v in vis:
        #     if prev_v is None or v != prev_v:
        #         print(v)
        #     prev_v = v

        # 2) RLE on debounced signal
        vals, lens, starts = _rle_bool(vis)
        if len(vals) == 0:
            continue

        evt_idx = 0
        for r in range(1, len(vals)):  # look at transitions; r is current run
            prev_v, prev_L, prev_s = vals[r-1], lens[r-1], starts[r-1]
            cur_v,  cur_L,  cur_s  = vals[r],   lens[r],   starts[r]

            # We want FALSE -> TRUE transitions only (re-entry)
            if (not prev_v) and cur_v:
                # Require the runs to be long enough (already debounced, but keep as guardrails)
                if prev_L < min_false_len or cur_L < min_true_len:
                    continue

                # 3) Choose indices for before/after medians
                #    - "before": last 'pre_window' samples of the preceding FALSE run
                pre_lo = max(prev_s, prev_s + prev_L - pre_window)
                pre_hi = prev_s + prev_L
                #    - "after": first 'post_window' samples of the current TRUE run
                post_lo = cur_s
                post_hi = min(cur_s + post_window, cur_s + cur_L)

                pre_idx  = np.arange(pre_lo,  pre_hi,  dtype=int)
                post_idx = np.arange(post_lo, post_hi, dtype=int)
                if pre_idx.size == 0 or post_idx.size == 0:
                    continue

                # y_before = float(np.nanmedian(y[pre_idx])); z_before = float(np.nanmedian(z[pre_idx]))
                # y_after  = float(np.nanmedian(y[post_idx])); z_after  = float(np.nanmedian(z[post_idx]))
                if point_estimation_type == "mean":
                    y_before = float(np.mean(y[pre_idx])); z_before = float(np.mean(z[pre_idx]))
                    y_after  = float(np.mean(y[post_idx])); z_after  = float(np.mean(z[post_idx]))
                elif point_estimation_type == "weighted_mean":
                    w_before = np.linspace(1.0, 0.1, len(pre_idx))
                    w_after  = np.linspace(0.1, 1.0, len(post_idx))
                    y_before = float(np.average(y[pre_idx], weights=w_before)); z_before = float(np.average(z[pre_idx], weights=w_before))
                    y_after  = float(np.average(y[post_idx], weights=w_after));  z_after  = float(np.average(z[post_idx], weights=w_after))
                elif point_estimation_type == "single":
                    y_before = float(y[pre_idx[0]]); z_before = float(z[pre_idx[0]])
                    y_after  = float(y[post_idx[-1]]); z_after  = float(z[post_idx[-1]])
                else:
                    raise ValueError("point_estimation_type must be 'mean', 'weighted_mean', or 'single'")
                dy = y_after - y_before; dz = z_after - z_before
                jump = float(np.hypot(dy, dz))

                events.append({
                    "line_idx": i,
                    "event_idx": evt_idx,
                    "t_enter": float(t[cur_s]),
                    "y_before": y_before, "z_before": z_before,
                    "y_after":  y_after,  "z_after":  z_after,
                    "dy": dy, "dz": dz, "jump_norm": jump
                })
                evt_idx += 1
                
    # Keep only events for each line_idx with the largest displacement between before and after:
    for i in sorted(set(e["line_idx"] for e in events)):
        E = [e for e in events if e["line_idx"] == i]
        if not E:
            continue
        max_jump = max(e["jump_norm"] for e in E)
        new_event = max(E, key=lambda e: e["jump_norm"])
        events = [e for e in events if e["line_idx"] != i]
        events.append(new_event)

    return pd.DataFrame(events).sort_values(["line_idx","t_enter"]).reset_index(drop=True)

# ---------- 4) plot before/after points + displacement vectors ----------
def plot_jump_vectors(events_df, line_color_map, *, equal_aspect=True, save_path=None, show=True):
    """
    2D YZ plot: 'before' (open), 'after' (filled), and displacement vectors.
    """
    fig, ax = plt.subplots(figsize=(6,6))
    for i in sorted(events_df["line_idx"].unique()):
        E = events_df[events_df["line_idx"]==i]
        if E.empty: continue
        c = line_color_map.get(i, 'k')
        ax.scatter(E["y_before"], E["z_before"], facecolors='none', edgecolors=c, label=f"Cable {i} (before)")
        ax.scatter(E["y_after"],  E["z_after"],  color=c, label=f"Cable {i} (after)")
        ax.quiver(E["y_before"], E["z_before"], E["dy"], E["dz"], angles='xy', scale_units='xy', scale=1, color=c, alpha=0.9)

    ax.set_xlabel("Line-frame Y (m) [cross-track]")
    ax.set_ylabel("Line-frame Z (m) [vertical]")
    if equal_aspect:
        ax.set_aspect('equal', adjustable='box')
    ax.grid(True, linestyle='--', alpha=0.4)
    ax.legend(loc='best', fontsize=9)
    if save_path:
        fig.savefig(save_path)
    if show:
        plt.show()
    plt.close()

# ---------- 5) summarize jump sizes ----------
def summarize_jump_stats(events_df):
    """
    Returns a DataFrame with per-cable and overall stats for jump magnitude.
    """
    rows = []
    def stats(col):
        a = np.asarray(col, float)
        return dict(n=len(a), mean=np.nanmean(a), median=np.nanmedian(a),
                    std=np.nanstd(a), p10=np.nanpercentile(a,10),
                    p90=np.nanpercentile(a,90))
    # per cable
    for i in sorted(events_df["line_idx"].unique()):
        s = stats(events_df.loc[events_df["line_idx"]==i, "jump_norm"])
        s.update(line_idx=i, scope=f"Cable {i+1}")
        rows.append(s)
    # overall
    s = stats(events_df["jump_norm"])
    s.update(line_idx=None, scope="All cables")
    rows.append(s)
    # return pd.DataFrame(rows)[["scope","n","mean","median","std","p10","p90"]]
    df = pd.DataFrame(rows)[["scope","n","mean","median","std","p10","p90"]]
    print(df)


def sort_lines_by_world_z(df: pd.DataFrame):
    """
    Return a copy of df where the 'lines[i].*' columns are reindexed so that
    i increases with the (ascending) average world Z over the whole dataframe.

    Also assigns fresh IDs (0..n_lines-1) into 'lines[new_i].id'.
    """
    # 1) Discover all line indices and their column suffixes
    line_pat = re.compile(r"^lines\[(\d+)\]\.(.+)$")
    line_cols = {}  # {i: set of suffixes}
    for col in df.columns:
        m = line_pat.match(col)
        if m:
            i = int(m.group(1))
            suffix = m.group(2)
            line_cols.setdefault(i, set()).add(suffix)

    if not line_cols:
        raise ValueError("No 'lines[i].*' columns found.")

    # Ensure we have the world Z columns to sort by
    for i in line_cols:
        if "world_position.z" not in line_cols[i]:
            raise ValueError(f"Missing 'lines[{i}].world_position.z' column.")

    # 2) Compute average world Z per line and get sort order
    means = {i: df[f"lines[{i}].world_position.z"].mean() for i in line_cols}
    order = sorted(means.keys(), key=lambda i: means[i])  # ascending Z

    # Mapping old_index -> new_index (sorted rank)
    old_to_new = {old_i: new_i for new_i, old_i in enumerate(order)}

    # 3) Build a new dataframe:
    #    - keep non-line columns as-is
    #    - re-map all line columns to their new indices
    non_line_cols = [c for c in df.columns if not line_pat.match(c)]
    out = df[non_line_cols].copy()

    # Recreate line columns under new indices
    for old_i in order:
        new_i = old_to_new[old_i]
        for suffix in line_cols[old_i]:
            old_col = f"lines[{old_i}].{suffix}"
            new_col = f"lines[{new_i}].{suffix}"
            # Some suffixes might not exist in all datasets; guard just in case
            if old_col in df.columns:
                out[new_col] = df[old_col]

    # 4) Overwrite IDs to 0..n-1 by the new order
    for new_i in range(len(order)):
        id_col = f"lines[{new_i}].id"
        out[id_col] = new_i

    # 5) (Optional) reorder columns: keep original non-line order,
    #    then lines[0].*, lines[1].*, ...
    def line_sort_key(c):
        m = line_pat.match(c)
        if not m:
            return (0, -1, c)  # non-line first, keep their order by name
        return (1, int(m.group(1)), m.group(2))

    out = out.reindex(sorted(out.columns, key=line_sort_key), axis=1)

    return out

def enforce_y_up_from_top_to_bottom(df):
    """
    Ensure that in the powerline direction frame the vector (top->bottom)
    has positive y. If not, rotate the frame 180° around its z-axis by
    updating world_line_orientation.{x,y,z,w} for all rows.
    """
    # ---- 1) Identify top and bottom cables from already-sorted indices ----
    # We assume you've already reindexed to lines[0..3] with ascending mean Z.
    # Compute mean world positions per line index after reindexing:
    means = {}
    for i in range(4):
        means[i] = np.array([
            df[f"lines[{i}].world_position.x"].mean(),
            df[f"lines[{i}].world_position.y"].mean(),
            df[f"lines[{i}].world_position.z"].mean(),
        ])
    top = means[0]       # lowest z after your sorting
    bottom = means[3]    # highest z after your sorting
    delta_world = top - bottom

    # ---- 2) Pick a representative quaternion (frame -> world) ----
    # Use the first non-NaN row.
    row0 = df.dropna(subset=["world_line_orientation.x",
                             "world_line_orientation.y",
                             "world_line_orientation.z",
                             "world_line_orientation.w"]).iloc[0]
    qx, qy, qz, qw = float(row0["world_line_orientation.x"]), float(row0["world_line_orientation.y"]), \
                     float(row0["world_line_orientation.z"]), float(row0["world_line_orientation.w"])
    R_fw = _quat_to_R(qx, qy, qz, qw)         # frame -> world
    R_wf = R_fw.T                              # world -> frame

    # ---- 3) Check sign of y in the powerline frame ----
    delta_frame = R_wf @ delta_world
    if delta_frame[1] >= 0:
        # Already correct
        return df, False

    # ---- 4) Flip the frame: rotate 180° about frame z ----
    r180 = np.array([0.0, 0.0, 1.0, 0.0])  # (x,y,z,w) for 180° around +z
    # Update all quaternions: q' = q ⊗ r180
    qx_col, qy_col, qz_col, qw_col = "world_line_orientation.x", "world_line_orientation.y", \
                                     "world_line_orientation.z", "world_line_orientation.w"
    q_arr = df[[qx_col, qy_col, qz_col, qw_col]].to_numpy(dtype=float)
    q_flipped = np.vstack([_quat_mul(q, r180) for q in q_arr])
    df[qx_col], df[qy_col], df[qz_col], df[qw_col] = q_flipped[:,0], q_flipped[:,1], q_flipped[:,2], q_flipped[:,3]
    return df, True




# --------- CLI ---------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Merge powerline CSV with latest world->drone TF before each timestamp.")
    ap.add_argument("--pl_csv", required=True, type=Path, help="Path to powerline topic CSV")
    ap.add_argument("--tf_csv", required=True, type=Path, help="Path to TF topic CSV (/tf)")
    ap.add_argument("--source_frame", default="world", help="TF source frame (default: world)")
    ap.add_argument("--target_frame", default="drone", help="TF target frame (default: drone)")
    ap.add_argument("-o", type=Path, help="Optional output directory to save figures", default=None)
    return ap.parse_args()


def main():
    args = parse_args()
    merged = merge_powerline_with_tf(
        pl_csv=args.pl_csv,
        tf_csv=args.tf_csv,
        source_frame=args.source_frame,
        target_frame=args.target_frame,
    )
    # print(f"Merged shape: {merged.shape}")
    # print(f"Columns: {list(merged.columns)}")
    
    merged = sort_lines_by_world_z(merged)
    merged, res = enforce_y_up_from_top_to_bottom(merged)

    reentry_jumps = detect_reentry_jumps(merged)
    summarize_jump_stats(reentry_jumps)

    # plot_3d_overview_cables_and_drone_firstN(
    #     merged,
    #     save_path=(args.o / "overview_3d_cables_and_drone.pdf") if args.o else None,
    #     show=args.o is None,
    # )
    # plot_visibility_timeseries(
    #     merged, 
    #     reentry_jumps,
    #     line_color_map,
    #     save_path=(args.o / "visibility_timeseries.pdf") if args.o else None,
    #     show=args.o is None,
    # )
    # plot_cable_tracks_in_zy(
    #     merged, 
    #     line_color_map,
    #     save_path=(args.o / "cable_tracks_in_zy.pdf") if args.o else None,
    #     show=args.o is None,
    # )
    # plot_cable_tracks_3d_raw(
    #     merged, 
    #     line_color_map,
    #     save_path=(args.o / "cable_tracks_3d_raw.pdf") if args.o else None,
    #     show=args.o is None,
    # )
    # plot_variance_vs_distance(
    #     merged, 
    #     line_color_map,
    #     save_path=(args.o / "variance_vs_distance.pdf") if args.o else None,
    #     show=args.o is None,
    # )
    plot_variance_in_vs_out(
        merged,
        line_color_map,
        save_path=(args.o / "variance_in_vs_out.pdf") if args.o else None,
        show=args.o is None,
    )
    # plot_jump_vectors(
    #     reentry_jumps, 
    #     line_color_map,
    #     save_path=(args.o / "reentry_jump_vectors.pdf") if args.o else None,
    #     show=args.o is None,
    # )

if __name__ == "__main__":
    main()
