#!/usr/bin/env python3
"""The cable-camera mount of a powerline evaluation model, read from the model itself.

The evaluation models are generated (Gazebo-simulation-assets/scripts/create_powerline_eval_drone_variant.py); a
camera-mount profile (for example ``d4s_dc_drone_powerline_eval_c25_maxphase``) carries the cable camera at another
pitch.  The model's SDF is the one source of that mount: the static ``drone -> cable_camera`` transform of a run and
the estimator's camera extrinsic are both derived from it here, so they cannot disagree with what the simulator
renders.

  powerline_slam_camera_mount.py show MODEL
  powerline_slam_camera_mount.py apply MODEL PARAMETER_SET     write the mount into a run's own parameter set
  powerline_slam_camera_mount.py check MODEL RUNTIME_CONFIG    refuse a backend calibration made for another mount
"""
from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
MODELS = WORKSPACE_ROOT / "src/III-Drone-Simulation/Gazebo-simulation-assets/models"
MOUNT_PARAMETER = "/tf/sim/powerline_eval/drone_to_cable_camera"      # [x, y, z, yaw, pitch, roll]


def model_sdf(model: str) -> Path:
    if not model.startswith("d4s_dc_drone_powerline_eval"):
        raise ValueError(f"not a powerline evaluation model: {model}")
    path = MODELS / model / "model.sdf"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def camera_pose(model: str) -> list[float]:
    """SDF pose [x, y, z, roll, pitch, yaw] of the model's cable camera.  The evaluator's segmentation camera (when the
    model has one) and the camera pose both radar plugins use for the evaluator's camera truth must be the same pose."""
    root = ET.parse(model_sdf(model)).getroot()
    camera = root.find(".//sensor[@name='cable_camera']")
    if camera is None or camera.findtext("pose") is None:
        raise ValueError(f"{model}: no cable_camera sensor pose")
    text = camera.findtext("pose")
    others = [x.findtext("pose") for x in root.iter("sensor") if x.get("name") == "pylon_semantic_camera"]
    others += [x.findtext("camera_pose") for x in root.iter("plugin") if x.find("camera_pose") is not None]
    if len(others) < 2 or any(other != text for other in others):
        raise ValueError(f"{model}: the segmentation camera and the radar plugins do not carry the cable camera's pose {text!r}: {others}")
    pose = [float(value) for value in text.split()]
    if len(pose) != 6:
        raise ValueError(f"{model}: malformed camera pose {text!r}")
    return pose


def mount(model: str) -> list[float]:
    """The III mount [x, y, z, yaw, pitch, roll] of the model's cable camera."""
    x, y, z, roll, pitch, yaw = camera_pose(model)
    return [x, y, z, yaw, pitch, roll]


def apply(model: str, parameter_set: Path) -> dict:
    """Write the model's camera mount into a run's own parameter set (the six list lines of the mount parameter).
    A parameter set that already carries the mount is left byte for byte as it is."""
    wanted = mount(model)
    text = parameter_set.read_text()
    block = re.search(rf"^(    {re.escape(MOUNT_PARAMETER)}:\n)((?:    - .*\n){{6}})", text, re.M)
    if block is None:
        raise ValueError(f"{parameter_set}: no six-element {MOUNT_PARAMETER}")
    current = [float(line.split("- ", 1)[1]) for line in block.group(2).splitlines()]
    changed = current != wanted
    if changed:
        lines = "".join(f"    - {value!r}\n" for value in wanted)
        text = text[:block.start(2)] + lines + text[block.end(2):]
        parameter_set.write_text(text)
    stored = yaml.safe_load(parameter_set.read_text())["/**"]["ros__parameters"][MOUNT_PARAMETER]
    if [float(value) for value in stored] != wanted:
        raise RuntimeError(f"{parameter_set}: the mount was not stored as written")
    return {"model": model, "parameter": MOUNT_PARAMETER, "mount_xyz_ypr": wanted, "previous": current, "changed": changed}


def rotation(roll: float, pitch: float, yaw: float) -> list[float]:
    """Row-major rotation parent-from-sensor of an SDF pose (Rz(yaw) Ry(pitch) Rx(roll))."""
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    return [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr,
            sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr,
            -sp, cp * sr, cp * cr]


def check(model: str, runtime_config: Path, tolerance: float = 1e-9) -> dict:
    """The camera extrinsic of the calibration set a backend runtime configuration names must be the mount of the
    model the simulator renders; a calibration made for another mount is refused."""
    calibration = Path(json.loads(runtime_config.read_text())["calibration_dir"])
    extrinsics = json.loads((calibration / "camera_extrinsics.json").read_text())["extrinsics"]
    x, y, z, roll, pitch, yaw = camera_pose(model)
    stored_translation = [float(extrinsics["translation_m"][axis]) for axis in "xyz"]
    stored_rotation = [float(value) for value in extrinsics["rotation_matrix_parent_from_sensor_row_major"]]
    wanted_rotation = rotation(roll, pitch, yaw)
    error = max([abs(a - b) for a, b in zip(stored_translation, [x, y, z])] + [abs(a - b) for a, b in zip(stored_rotation, wanted_rotation)])
    result = {"model": model, "runtime_config": str(runtime_config), "calibration_dir": str(calibration),
              "model_sdf_pose_xyz_rpy": [x, y, z, roll, pitch, yaw], "calibration_translation_m": stored_translation,
              "calibration_rotation_row_major": stored_rotation, "max_abs_difference": error, "tolerance": tolerance,
              "matches": error <= tolerance}
    if not result["matches"]:
        raise ValueError(f"{calibration}: the camera extrinsic is not the mount of {model} (largest difference {error:.6g}); "
                         "a backend calibration made for another camera mount is refused")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("model")
    write = commands.add_parser("apply")
    write.add_argument("model")
    write.add_argument("parameter_set", type=Path)
    verify = commands.add_parser("check")
    verify.add_argument("model")
    verify.add_argument("runtime_config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "check":
        print(json.dumps(check(args.model, args.runtime_config)))
    elif args.command == "show":
        print(json.dumps({"model": args.model, "sdf_pose_xyz_rpy": camera_pose(args.model), "mount_xyz_ypr": mount(args.model)}))
    else:
        print(json.dumps(apply(args.model, args.parameter_set)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
