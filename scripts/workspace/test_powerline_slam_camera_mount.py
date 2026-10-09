import json
import math
import tempfile
import unittest
from pathlib import Path

import yaml

import powerline_slam_camera_mount as camera_mount
import powerline_slam_runtime_inputs as inputs

C20 = [0.0, -0.215, 0.3, 0.0, -1.2217304763960306, 0.0]
C25 = [0.0, -0.215, 0.3, 0.0, -1.1344640137963142, 0.0]


class CameraMountTest(unittest.TestCase):
    def test_the_mount_is_read_from_the_model(self) -> None:
        for model in ("d4s_dc_drone_powerline_eval", "d4s_dc_drone_powerline_eval_maxphase", "d4s_dc_drone_powerline_eval_maxphase_drift",
                      "d4s_dc_drone_powerline_eval_maxphase_longrun"):
            self.assertEqual(C20, camera_mount.mount(model))
        for model in ("d4s_dc_drone_powerline_eval_c25_maxphase", "d4s_dc_drone_powerline_eval_c25_maxphase_drift",
                      "d4s_dc_drone_powerline_eval_c25_maxphase_longrun"):
            self.assertEqual(C25, camera_mount.mount(model))
        self.assertAlmostEqual(-math.radians(65.0), C25[4], places=15)
        with self.assertRaises(ValueError):
            camera_mount.mount("d4s_dc_drone")

    def test_the_tracked_parameter_set_carries_the_c20_mount_and_is_left_alone_by_a_c20_model(self) -> None:
        tracked = inputs.SIM_PARAMETER_SET.read_text()
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "set.yaml"
            copy.write_text(tracked)
            result = camera_mount.apply("d4s_dc_drone_powerline_eval_maxphase_longrun", copy)
            self.assertFalse(result["changed"])
            self.assertEqual(tracked, copy.read_text())

    def test_a_c25_model_writes_its_mount_and_nothing_else(self) -> None:
        tracked = inputs.SIM_PARAMETER_SET.read_text()
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "set.yaml"
            copy.write_text(tracked)
            result = camera_mount.apply("d4s_dc_drone_powerline_eval_c25_maxphase", copy)
            self.assertTrue(result["changed"])
            self.assertEqual(C20, result["previous"])
            before = yaml.safe_load(tracked)["/**"]["ros__parameters"]
            after = yaml.safe_load(copy.read_text())["/**"]["ros__parameters"]
            self.assertEqual(C25, after[camera_mount.MOUNT_PARAMETER])
            self.assertEqual({k: v for k, v in before.items() if k != camera_mount.MOUNT_PARAMETER},
                             {k: v for k, v in after.items() if k != camera_mount.MOUNT_PARAMETER})
            differing = [(a, b) for a, b in zip(tracked.splitlines(), copy.read_text().splitlines()) if a != b]
            self.assertEqual([("    - -1.2217304763960306", "    - -1.1344640137963142")], differing)
            self.assertFalse(camera_mount.apply("d4s_dc_drone_powerline_eval_c25_maxphase", copy)["changed"])


class CameraMountCalibrationTest(unittest.TestCase):
    def build(self, root: Path, name: str, model: str | None) -> Path:
        doppler = root / "doppler"
        doppler.mkdir(exist_ok=True)
        for file_name in inputs.DOPPLER_FILES:
            (doppler / file_name).write_text(json.dumps({"name": file_name}))
        output = root / name
        inputs.build_calibration(output, doppler, model)
        return output

    def test_a_c25_calibration_differs_from_the_nominal_one_in_the_camera_extrinsic_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nominal = self.build(root, "nominal", None)
            same = self.build(root, "c20_model", "d4s_dc_drone_powerline_eval_maxphase")
            c25 = self.build(root, "c25", "d4s_dc_drone_powerline_eval_c25_maxphase")
            for name in ("camera_intrinsics.yaml", "radar_extrinsics.yaml", "radar_up_extrinsics.yaml", "radar_forward_extrinsics.yaml",
                         *inputs.DOPPLER_FILES):
                self.assertEqual((nominal / name).read_bytes(), (c25 / name).read_bytes(), name)
            self.assertEqual((nominal / "camera_extrinsics.json").read_bytes(), (same / "camera_extrinsics.json").read_bytes())
            a = json.loads((nominal / "camera_extrinsics.json").read_text())["extrinsics"]
            b = json.loads((c25 / "camera_extrinsics.json").read_text())["extrinsics"]
            self.assertEqual({k: v for k, v in a.items() if k not in ("rotation_matrix_parent_from_sensor_row_major", "source_pose_rpy_rad")},
                             {k: v for k, v in b.items() if k not in ("rotation_matrix_parent_from_sensor_row_major", "source_pose_rpy_rad")})
            pitch = -math.radians(65.0)
            expected = [math.cos(pitch), 0.0, math.sin(pitch), 0.0, 1.0, 0.0, -math.sin(pitch), 0.0, math.cos(pitch)]
            for got, want in zip(b["rotation_matrix_parent_from_sensor_row_major"], expected):
                self.assertAlmostEqual(want, got, places=15)
            self.assertAlmostEqual(pitch, b["source_pose_rpy_rad"][1], places=12)
            # the camera's optical axis (the Gazebo sensor's +x) in the drone frame: 25 deg forward from upward
            axis = b["rotation_matrix_parent_from_sensor_row_major"][0::3]
            self.assertAlmostEqual(math.sin(math.radians(25.0)), axis[0], places=12)
            self.assertAlmostEqual(math.cos(math.radians(25.0)), axis[2], places=12)
            manifest = json.loads((c25 / "SOURCE_MANIFEST.json").read_text())
            self.assertEqual("iii_d4s_dc_drone_powerline_eval_c25_maxphase", manifest["variant"])
            self.assertEqual([0.0, -0.215, 0.3, 0.0, pitch, 0.0], manifest["camera_mount"]["mount_xyz_ypr"])
            self.assertTrue(manifest["derived_from"]["variant_model"]["path"].endswith("d4s_dc_drone_powerline_eval_c25_maxphase/model.sdf"))
            self.assertNotIn("camera_mount", json.loads((nominal / "SOURCE_MANIFEST.json").read_text()))


if __name__ == "__main__":
    unittest.main()
