#!/usr/bin/env python3
"""Focused tests for the powerline SLAM corridor flight tooling."""

from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("powerline_slam_flights.py")
SPEC = importlib.util.spec_from_file_location("powerline_slam_flights", SCRIPT)
assert SPEC and SPEC.loader
flights = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = flights
SPEC.loader.exec_module(flights)

MAPPING = {"offset": {"x": 0.5, "y": -0.25, "z": 0.1}}


class CatalogTest(unittest.TestCase):
    def test_catalog_has_both_directions_with_unique_legs(self) -> None:
        catalog = flights.load_catalog()
        self.assertEqual({"a_to_b", "b_to_a"}, set(catalog["flights"]))
        for flight in catalog["flights"].values():
            names = [leg["name"] for leg in flight["legs"]]
            self.assertEqual(11, len(names))
            self.assertEqual(len(names), len(set(names)))
        self.assertEqual("d4s_dc_drone_powerline_eval", catalog["sensor_layout"])

    def test_exact_topic_contract(self) -> None:
        self.assertEqual(29, len(flights.RECORD_TOPICS))
        self.assertEqual(len(flights.RECORD_TOPICS), len(set(flights.RECORD_TOPICS)))
        self.assertTrue(flights.EMPTY_BY_CONTRACT <= set(flights.RUNTIME_TOPICS))
        self.assertIn("/simulation/gazebo/imu", flights.RECORD_TOPICS)
        # The flight waits for the per-frame camera truth it records.
        self.assertTrue(set(flights.CAMERA_TRUTH_DRAIN_TOPICS) <= set(flights.TRUTH_TOPICS))
        # The ground segment records the IMU and the arming state, not the flight contract.
        self.assertEqual(
            {"/clock", "/fmu/out/sensor_combined", "/fmu/out/vehicle_status_v1", "/simulation/gazebo/imu",
             "/simulation/ground_truth/drone/state"},
            set(flights.GROUND_IMU_TOPICS),
        )
        for topic in (
            "/sensor/mmwave/points_full",
            "/sensor/mmwave_forward/points_full",
            "/sensor/cable_camera/camera_info",
            "/fmu/out/sensor_combined",
        ):
            self.assertIn(topic, flights.RUNTIME_TOPICS)
        for topic in flights.TRUTH_TOPICS:
            self.assertTrue(topic.startswith("/simulation/ground_truth/"))

    def test_images_and_streams_have_recorders_of_their_own(self) -> None:
        self.assertEqual(set(flights.RECORD_TOPICS), set(flights.IMAGE_TOPICS) | set(flights.STREAM_TOPICS))
        self.assertFalse(set(flights.IMAGE_TOPICS) & set(flights.STREAM_TOPICS))
        self.assertEqual(26, len(flights.STREAM_TOPICS))
        # The streams' recorder takes no camera-resolution image.
        for topic in flights.STREAM_TOPICS:
            self.assertNotIn("image", topic)
            self.assertNotIn("mask", topic)

    def test_recorder_transport_losses_from_its_log(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "rosbag_stderr.log"
            log.write_text("[INFO] Recording stopped\n[WARN] [1.0] [rosbag2_recorder]: "
                           "Number of messages lost on the transport layer: 263\n")
            self.assertEqual(263, flights.reported_transport_losses(str(log)))
            log.write_text("[INFO] Recording stopped\n")
            self.assertEqual(0, flights.reported_transport_losses(str(log)))
            self.assertIsNone(flights.reported_transport_losses(str(Path(directory) / "missing.log")))
        self.assertIsNone(flights.reported_transport_losses(None))

    def test_cli_defaults_fly_both_directions(self) -> None:
        args = flights.build_parser().parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertEqual(["a_to_b", "b_to_a"], args.flights)
        self.assertFalse(args.keep_running)
        # Each flight flies in its own process unless --single-process.
        self.assertFalse(args.single_process)
        self.assertFalse(args.append)


class FrameTest(unittest.TestCase):
    def test_gazebo_east_is_live_minus_y(self) -> None:
        zero = {"offset": {"x": 0.0, "y": 0.0, "z": 0.0}}
        self.assertEqual({"x": 0.0, "y": -1.0, "z": 0.0}, flights.gazebo_to_live_world([1.0, 0.0, 0.0], zero))
        self.assertEqual({"x": 1.0, "y": 0.0, "z": 0.0}, flights.gazebo_to_live_world([0.0, 1.0, 0.0], zero))

    def test_live_mapping_round_trip(self) -> None:
        point = [7.8, -2.9, 0.8]
        live = flights.gazebo_to_live_world(point, MAPPING)
        for expected, actual in zip(point, flights.live_world_to_gazebo(live, MAPPING)):
            self.assertAlmostEqual(expected, actual, places=12)


class PlanTest(unittest.TestCase):
    def test_planned_command_is_the_flown_command(self) -> None:
        catalog = flights.load_catalog()
        minimum = float(catalog["minimum_live_height_m"])
        plan = flights.plan_flight(catalog, "a_to_b", MAPPING)
        legs = catalog["flights"]["a_to_b"]["legs"]
        self.assertEqual([leg["name"] for leg in legs], [row["name"] for row in plan])
        for leg, row in zip(legs, plan):
            live = row["live_target"]
            self.assertGreaterEqual(live["z"], minimum)
            self.assertEqual(list(leg["target_world_m"]), row["catalog_command"]["target_world_m"])
            flown = flights.live_world_to_gazebo(live, MAPPING)
            for expected, actual in zip(flown, row["planned_command"]["target_world_m"]):
                self.assertAlmostEqual(expected, actual, places=6)
            self.assertAlmostEqual(math.remainder(-float(leg["yaw_rad"]) - live["yaw"], 2 * math.pi), 0.0)
            self.assertEqual(row["catalog_command"]["yaw_rad"], row["planned_command"]["yaw_rad"])

    def test_low_catalog_targets_are_raised_to_the_live_floor(self) -> None:
        catalog = flights.load_catalog()
        plan = flights.plan_flight(catalog, "a_to_b", {"offset": {"x": 0.0, "y": 0.0, "z": 0.0}})
        raised = [row for row in plan if row["catalog_command"]["target_world_m"][2] < catalog["minimum_live_height_m"]]
        self.assertTrue(raised)
        for row in raised:
            self.assertAlmostEqual(catalog["minimum_live_height_m"], row["planned_command"]["target_world_m"][2])


class EvidenceTest(unittest.TestCase):
    def test_contiguous_source_time_ownership(self) -> None:
        plan = flights.plan_flight(flights.load_catalog(), "b_to_a", MAPPING)
        begins = [10_000_000_000 + index * 1_000_000_000 for index in range(len(plan))]
        evidence = flights.mission_phase_evidence("b_to_a", plan, begins, begins[-1] + 500_000_000, "0" * 64)
        self.assertEqual("VALID", evidence["status"])
        self.assertEqual("nanoseconds", evidence["source_time_unit"])
        phases = evidence["planned_phases"]
        self.assertEqual(len(plan), len(phases))
        for left, right in zip(phases, phases[1:]):
            self.assertEqual(left["source_time_interval_ns"]["end"], right["source_time_interval_ns"]["begin"])
        self.assertEqual(begins[-1] + 500_000_000, phases[-1]["source_time_interval_ns"]["end"])
        self.assertTrue(all(phase["status"] == "COMPLETE" for phase in phases))

    def test_rejects_non_increasing_boundaries(self) -> None:
        plan = flights.plan_flight(flights.load_catalog(), "a_to_b", MAPPING)[:2]
        with self.assertRaises(ValueError):
            flights.mission_phase_evidence("a_to_b", plan, [5, 5], 9, "0" * 64)
        with self.assertRaises(ValueError):
            flights.mission_phase_evidence("a_to_b", plan, [5], 9, "0" * 64)


class BagStoragePresetTest(unittest.TestCase):
    def test_the_merged_bag_is_uncompressed_unless_a_known_preset_is_asked_for(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("III_POWERLINE_BAG_STORAGE_PRESET", None)
            self.assertIsNone(flights.bag_storage_preset())
        with mock.patch.dict(os.environ, {"III_POWERLINE_BAG_STORAGE_PRESET": "zstd_fast"}):
            self.assertEqual("zstd_fast", flights.bag_storage_preset())
        with mock.patch.dict(os.environ, {"III_POWERLINE_BAG_STORAGE_PRESET": "gzip"}):
            with self.assertRaises(RuntimeError):
                flights.bag_storage_preset()


if __name__ == "__main__":
    unittest.main()
