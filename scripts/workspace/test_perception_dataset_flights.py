#!/usr/bin/env python3
"""Focused tests for the workspace perception dataset flight tooling."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPT = Path(__file__).with_name("perception_dataset_flights.py")
SPEC = importlib.util.spec_from_file_location("perception_dataset_flights", SCRIPT)
assert SPEC and SPEC.loader
suite = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = suite
SPEC.loader.exec_module(suite)


class CatalogTest(unittest.TestCase):
    def test_headless_cli_is_available(self) -> None:
        args = suite.build_parser().parse_args(["--headless", "--dry-run"])
        self.assertTrue(args.headless)

    def test_catalog_and_exact_topic_contract(self) -> None:
        self.assertEqual([], suite.validate_catalog())
        self.assertEqual(37, len(suite.RECORD_TOPICS))
        self.assertEqual(37, len(set(suite.RECORD_TOPICS)))
        self.assertNotIn("/sensor/cable_camera/camera_info", suite.RECORD_TOPICS)
        self.assertNotIn("/fmu/in/trajectory_setpoint", suite.RECORD_TOPICS)
        self.assertIn("/fmu/out/vehicle_status_v1", suite.RECORD_TOPICS)
        self.assertIn("/fmu/out/vehicle_imu", suite.RECORD_TOPICS)
        self.assertIn("/fmu/out/sensor_combined", suite.RECORD_TOPICS)
        self.assertIn("/sensor/mmwave/points_full", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/drone/odometry", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/mmwave/conductor_labels", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/cable_camera/conductor_instance_mask", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/conductor_id_map", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/drone/state", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/conductors/geometry", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/mmwave/scan", suite.RECORD_TOPICS)
        self.assertIn("/simulation/ground_truth/cable_camera/frame", suite.RECORD_TOPICS)
        self.assertNotIn("/fmu/out/vehicle_status", suite.RECORD_TOPICS)

    def test_scenarios_are_stable_and_most_are_in_corridor(self) -> None:
        self.assertEqual([f"B{index:02d}" for index in range(1, 19)], [item.scenario_id for item in suite.SCENARIOS])
        self.assertGreater(sum(item.expected_inside_corridor_majority for item in suite.SCENARIOS), 9)
        self.assertEqual(len(suite.SCENARIOS), len({item.folder_name for item in suite.SCENARIOS}))

    def test_b16_is_full_partial_full_in_one_bag(self) -> None:
        scenario = suite.select_scenarios(["B16"])[0]
        self.assertEqual(("full", "partial", "full"), scenario.expected_observability_sequence)
        self.assertEqual(("full", "partial", "full"), tuple(item.expected_visibility for item in scenario.waypoints))
        self.assertTrue(suite.ordered_states_present(["full", "full", "partial", "full"], scenario.expected_observability_sequence))
        self.assertFalse(suite.ordered_states_present(["partial", "full"], scenario.expected_observability_sequence))

    def test_coherence_uses_sampled_visibility_not_waypoint_labels(self) -> None:
        scenario = suite.select_scenarios(["B16"])[0]
        samples = [
            {
                "x": float(index), "y": 0.0, "z": 0.0, "yaw": 0.0,
                "elapsed_sec": float(index), "nearest_conductor_distance_m": 1.0,
                "inside_powerline_corridor": True, "target_index": min(index, 2),
                "phase": "hold", "expected_visible_count": 4,
            }
            for index in range(10)
        ]
        targets = [
            {"x": 0.0, "y": 0.0, "z": 0.0, "label": "a", "expected_visibility": "full"},
            {"x": 1.0, "y": 0.0, "z": 0.0, "label": "b", "expected_visibility": "partial"},
            {"x": 2.0, "y": 0.0, "z": 0.0, "label": "c", "expected_visibility": "full"},
        ]
        report = suite.coherence_report(scenario, samples, targets)
        self.assertFalse(report["checks"]["required_observability_order"])
        self.assertEqual(["full"], report["metrics"]["sampled_observability_transitions"])

    def test_manifest_serialization_and_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir, manifest = suite.initialize_run(
                Path(temp), "test", suite.select_scenarios(["B01", "B16"]), 1,
                suite.DEFAULT_GEOMETRY_PATH, dry_run=True,
            )
            self.assertEqual(2, manifest["scenario_count"])
            self.assertTrue((run_dir / "manifest.json").is_file())
            self.assertTrue((run_dir / "manifest.csv").is_file())
            truth = json.loads((run_dir / "B16_full_partial_full/ground_truth.json").read_text())
            self.assertEqual(4, truth["conductors"]["aggregate"]["conductor_count"])
            self.assertEqual(4, len(truth["conductors"]["conductors"]))
            self.assertIn("cable_camera", truth["sensors"])
            self.assertIn("mmwave", truth["sensors"])
            self.assertEqual(
                {"x", "y", "z", "velocity", "snr", "noise"},
                set(truth["sensors"]["mmwave"]["full_topic_fields"]),
            )
            self.assertEqual(
                "/simulation/ground_truth/drone/state",
                truth["vehicle_ground_truth"]["topic"],
            )
            self.assertEqual(
                ["conductor_1", "conductor_2", "conductor_3", "conductor_4"],
                truth["measurement_provenance"]["immutable_ids"],
            )

    def test_gazebo_geometry_maps_into_live_ros_world(self) -> None:
        geometry = json.loads(suite.DEFAULT_GEOMETRY_PATH.read_text())
        mapped = suite.map_geometry_data_to_live_ros(
            geometry,
            {"offset": {"x": 10.0, "y": 20.0, "z": 30.0, "yaw": 0.0}},
        )
        source = geometry["ground_truth"]["powerlines"]["conductors"][0]["samples"][0]
        result = mapped["ground_truth"]["powerlines"]["conductors"][0]["samples"][0]
        self.assertAlmostEqual(source["y"] + 10.0, result["x"])
        self.assertAlmostEqual(-source["x"] + 20.0, result["y"])
        self.assertAlmostEqual(source["z"] + 30.0, result["z"])
        self.assertEqual("live_ros_world", mapped["ground_truth"]["powerlines"]["coordinate_space"])


class BagMetadataTest(unittest.TestCase):
    def write_metadata(self, directory: Path, topics: list[str], *, zero_topic: str | None = None) -> Path:
        entries = [
            {
                "topic_metadata": {"name": name, "type": "example/msg/T", "serialization_format": "cdr"},
                "message_count": 0 if name == zero_topic else 1,
            }
            for name in topics
        ]
        metadata = {
            "rosbag2_bagfile_information": {
                "duration": {"nanoseconds": 1}, "message_count": sum(item["message_count"] for item in entries),
                "topics_with_message_count": entries,
            }
        }
        path = directory / "metadata.yaml"
        path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
        return path

    def test_exact_topic_metadata_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = suite.verify_exact_bag_topics(self.write_metadata(Path(temp), list(suite.RECORD_TOPICS)))
            self.assertTrue(result["success"])

    def test_missing_extra_and_zero_topics_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            topics = list(suite.RECORD_TOPICS[1:]) + ["/unexpected"]
            result = suite.verify_exact_bag_topics(self.write_metadata(Path(temp), topics, zero_topic=topics[0]))
            self.assertFalse(result["success"])
            self.assertEqual([suite.RECORD_TOPICS[0]], result["missing_topics"])
            self.assertEqual(["/unexpected"], result["unexpected_topics"])
            self.assertEqual([topics[0]], result["zero_message_topics"])

    def test_explicit_legacy_contract_remains_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            legacy_topics = [
                topic for topic in suite.RECORD_TOPICS
                if topic not in {
                    "/sensor/mmwave/points_full",
                    "/simulation/ground_truth/drone/odometry",
                    "/simulation/ground_truth/mmwave/conductor_labels",
                    "/simulation/ground_truth/cable_camera/conductor_instance_mask",
                    "/simulation/ground_truth/conductor_id_map",
                    "/simulation/ground_truth/drone/state",
                    "/simulation/ground_truth/conductors/geometry",
                    "/simulation/ground_truth/mmwave/scan",
                    "/simulation/ground_truth/cable_camera/frame",
                }
            ]
            result = suite.verify_exact_bag_topics(
                self.write_metadata(Path(temp), legacy_topics), legacy_topics
            )
            self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
