#!/usr/bin/env python3
"""Focused tests for the powerline SLAM estimator input generator."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPT = Path(__file__).with_name("powerline_slam_runtime_inputs.py")
SPEC = importlib.util.spec_from_file_location("powerline_slam_runtime_inputs", SCRIPT)
assert SPEC and SPEC.loader
inputs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inputs
SPEC.loader.exec_module(inputs)

SECOND = 1_000_000_000


def evidence(yaws: list[float]) -> dict:
    phases = []
    for index, yaw in enumerate(yaws):
        phases.append({
            "name": f"leg_{index}" + ("_hold" if index % 2 else ""),
            "status": "COMPLETE",
            "planned_command": {"frame": "world", "target_world_m": [float(index), 2.0, 1.0],
                                "yaw_rad": yaw, "duration_s": 10.0},
            "source_time_interval_ns": {"begin": (10 + 10 * index) * SECOND, "end": (20 + 10 * index) * SECOND},
        })
    return {"status": "VALID", "source_time_unit": "nanoseconds", "planned_phases": phases}


def write(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document))
    return path


class RotationTest(unittest.TestCase):
    def test_ypr_quaternion_and_rpy_agree(self) -> None:
        rotation = inputs.rotation_ypr(0.3, -0.6981317007977318, 0.1)
        x, y, z, w = inputs.quaternion_xyzw(rotation)
        self.assertAlmostEqual(1.0, x * x + y * y + z * z + w * w)
        roll, pitch, yaw = inputs.roll_pitch_yaw(rotation)
        self.assertAlmostEqual(0.1, roll)
        self.assertAlmostEqual(-0.6981317007977318, pitch)
        self.assertAlmostEqual(0.3, yaw)

    def test_forward_radar_extrinsic_is_a_pure_pitch(self) -> None:
        document = inputs.extrinsics([0.105, -0.24, 0.285, 0.0, -0.6981317007977318, 0.0], "mmwave_forward")
        x, y, z, w = document["quaternion_xyzw"]
        self.assertAlmostEqual(0.0, x)
        self.assertAlmostEqual(-math.sin(0.6981317007977318 / 2), y)
        self.assertAlmostEqual(0.0, z)
        self.assertAlmostEqual(math.cos(0.6981317007977318 / 2), w)
        self.assertEqual("mmwave_forward", document["child_frame"])


class CalibrationTest(unittest.TestCase):
    def test_builds_a_complete_set_from_the_sim_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doppler = root / "doppler"
            doppler.mkdir()
            for name in inputs.DOPPLER_FILES:
                write(doppler / name, {"name": name})
            output = root / "calibration"
            manifest = inputs.build_calibration(output, doppler)
            expected = {
                "camera_extrinsics.json", "camera_intrinsics.yaml", "radar_extrinsics.yaml",
                "radar_up_extrinsics.yaml", "radar_forward_extrinsics.yaml", *inputs.DOPPLER_FILES,
            }
            self.assertEqual(expected, set(manifest["files_sha256"]))
            self.assertEqual((output / "radar_up_extrinsics.yaml").read_bytes(),
                             (output / "radar_extrinsics.yaml").read_bytes())
            params = inputs.sim_parameters()
            forward = yaml.safe_load((output / "radar_forward_extrinsics.yaml").read_text())["radar_extrinsics"]
            self.assertEqual(params["/tf/sim/powerline_eval/drone_to_mmwave_forward"][:3], forward["translation_m"])
            intrinsics = yaml.safe_load((output / "camera_intrinsics.yaml").read_text())
            width = intrinsics["image_width"]
            focal = intrinsics["camera_matrix"]["data"][0]
            self.assertGreater(focal, 0.0)
            self.assertAlmostEqual((width - 1) / 2, intrinsics["camera_matrix"]["data"][2])
            with self.assertRaises(SystemExit):
                inputs.build_calibration(output, doppler)


class MissionPriorTest(unittest.TestCase):
    def test_settled_priors_skip_the_first_leg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write(Path(directory) / "evidence.json", evidence([math.pi / 2, math.pi / 2, 0.0]))
            document = inputs.build_mission_priors(path, 0.3, 0.03)
        self.assertEqual(4, document["schema_version"])
        priors = document["priors"]
        self.assertEqual(2, len(priors))
        self.assertEqual(25 * SECOND, priors[0]["valid_from_source_time_ns"])
        self.assertEqual(30 * SECOND - 1, priors[0]["valid_until_source_time_ns"])
        self.assertEqual(40 * SECOND, priors[1]["valid_until_source_time_ns"])
        self.assertEqual([[10 * SECOND, 20 * SECOND - 1], [20 * SECOND, 25 * SECOND - 1], [30 * SECOND, 35 * SECOND - 1]],
                         document["excluded_no_anchor_intervals_source_time_ns"])
        # NED heading east is ENU +x: identity from drone FLU.
        for row, expected in zip(priors[0]["rotation_map_from_drone"], ((1, 0, 0), (0, 1, 0), (0, 0, 1))):
            for actual, value in zip(row, expected):
                self.assertAlmostEqual(value, actual)
        self.assertAlmostEqual(0.09, priors[0]["covariance_map_pose"][0][0])
        self.assertAlmostEqual(0.0009, priors[0]["covariance_map_pose"][5][5])

    def test_world_frame_priors_follow_the_live_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write(root / "evidence.json", evidence([0.0, 0.4]))
            plan = write(root / "flight_plan.json", {"live_mapping": {"offset": {"x": 1.0, "y": 2.0, "z": 0.5}}})
            prior = inputs.build_mission_priors(path, 0.3, 0.03, plan)["priors"][0]
        self.assertEqual("world", prior["map_frame_id"])
        self.assertEqual([2.0 + 1.0, -1.0 + 2.0, 1.0 + 0.5], prior["commanded_position_map_m"])
        # III world is north-west-up: yaw is the negated NED heading.
        rotation = prior["rotation_map_from_drone"]
        self.assertAlmostEqual(math.cos(-0.4), rotation[0][0])
        self.assertAlmostEqual(math.sin(-0.4), rotation[1][0])

    def test_rejects_invalid_evidence(self) -> None:
        document = evidence([0.0, 0.0])
        document["planned_phases"][1]["source_time_interval_ns"]["begin"] += 1
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                inputs.build_mission_priors(write(Path(directory) / "gap.json", document), 0.3, 0.03)
            document = evidence([0.0, 0.0])
            document["status"] = "INVALID"
            with self.assertRaises(SystemExit):
                inputs.build_mission_priors(write(Path(directory) / "invalid.json", document), 0.3, 0.03)


class MapPriorTest(unittest.TestCase):
    PRIOR = {
        "map_frame_id": inputs.MAP_FRAME,
        "landmarks": [
            {"map_id": "pylon_a", "position_map_m": [1.0, 2.0, 3.0],
             "covariance_global_m2": [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]]},
        ],
        "joint_covariance_policy": {
            "map_frame_id": inputs.MAP_FRAME,
            "landmark_ids": ["pylon_a"],
            "landmark_joint_covariance_global_m2": [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]],
            "registration_landmark_cross_covariance": [[0.1, 0.0, 0.0]] + [[0.0, 0.0, 0.0]] * 5,
        },
    }

    def test_rigid_conversion_to_the_live_world(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = write(root / "map_prior.json", self.PRIOR)
            plan = write(root / "flight_plan.json", {"live_mapping": {"offset": {"x": 0.5, "y": -0.5, "z": 0.25}}})
            converted = inputs.convert_map_prior(source, plan)
        self.assertEqual("world", converted["map_frame_id"])
        landmark = converted["landmarks"][0]
        self.assertEqual([2.5, -1.5, 3.25], landmark["position_map_m"])
        self.assertEqual([[2.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 3.0]], landmark["covariance_global_m2"])
        policy = converted["joint_covariance_policy"]
        self.assertEqual("world", policy["map_frame_id"])
        self.assertEqual(landmark["covariance_global_m2"], policy["landmark_joint_covariance_global_m2"])
        # Registration x pairs with landmark x: both axes rotate, so the term moves to (y, y).
        cross = policy["registration_landmark_cross_covariance"]
        self.assertAlmostEqual(0.1, cross[1][1])
        self.assertAlmostEqual(0.0, cross[0][0])

    def test_rejects_a_prior_in_another_frame(self) -> None:
        prior = copy.deepcopy(self.PRIOR)
        prior["map_frame_id"] = "world"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = write(root / "flight_plan.json", {"live_mapping": {"offset": {"x": 0.0, "y": 0.0, "z": 0.0}}})
            with self.assertRaises(SystemExit):
                inputs.convert_map_prior(write(root / "map_prior.json", prior), plan)


class GroundCovarianceTest(unittest.TestCase):
    @staticmethod
    def samples(gyro_mean: float = 0.0, count: int = 6000, seed: int = 7) -> list:
        import numpy as np

        rng = np.random.default_rng(seed)
        return [
            (index * 10_000_000, rng.normal(gyro_mean, 2e-4, 3), np.array([0.0, 0.0, 9.81]) + rng.normal(0.0, 6e-3, 3))
            for index in range(count)
        ]

    def test_disarmed_ground_segment_measures_sensor_noise(self) -> None:
        document = inputs.ground_gyro_covariance(self.samples(), [inputs.ARMING_STATE_DISARMED] * 30)
        covariance = document["angular_velocity_covariance_drone_rad2ps2"]
        for axis in range(3):
            self.assertAlmostEqual(4e-8, covariance[axis][axis], delta=4e-9)
        self.assertEqual("disarmed_ground", document["source"])
        (window,) = document["stationary_windows"]
        self.assertEqual([inputs.HOLD_SETTLE_NS, 59_990_000_000 - inputs.HOLD_END_GUARD_NS],
                         window["source_time_window_ns"])
        self.assertNotIn("centered", window)

    def test_rejects_an_armed_or_moving_segment(self) -> None:
        with self.assertRaises(SystemExit):
            inputs.ground_gyro_covariance(self.samples(), [inputs.ARMING_STATE_DISARMED, 2])
        with self.assertRaises(SystemExit):
            inputs.ground_gyro_covariance(self.samples(), [])
        with self.assertRaises(SystemExit):
            inputs.ground_gyro_covariance(self.samples(gyro_mean=0.05), [inputs.ARMING_STATE_DISARMED])
        with self.assertRaises(SystemExit):
            inputs.ground_gyro_covariance(self.samples(count=1000), [inputs.ARMING_STATE_DISARMED])


class StationaryWindowTest(unittest.TestCase):
    def test_guarded_hold_windows(self) -> None:
        windows = inputs.stationary_windows(evidence([0.0] * 8))
        self.assertEqual(4, len(windows))
        name, begin, end = windows[0]
        self.assertEqual("leg_1_hold", name)
        self.assertEqual(20 * SECOND + inputs.HOLD_SETTLE_NS, begin)
        self.assertEqual(30 * SECOND - inputs.HOLD_END_GUARD_NS, end)

    def test_requires_four_holds(self) -> None:
        with self.assertRaises(SystemExit):
            inputs.stationary_windows(evidence([0.0] * 4))


if __name__ == "__main__":
    unittest.main()
