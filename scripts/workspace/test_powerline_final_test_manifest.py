#!/usr/bin/env python3
"""Regression checks for frozen FINALTEST manifest materialization."""
import json
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = Path("/home/ffn/Workspace/disturbance_nmpc/datasets/powerline_qualification_final_test")
TEMPLATE = Path("/home/ffn/Workspace/disturbance_nmpc/powerline_perception/schemas/powerline_final_test_manifest_template.json")


def required_paths(value, prefix=""):
    paths = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= required_paths(child, path)
    return paths


def present_paths(value, prefix=""):
    paths = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= present_paths(child, path)
    return paths


class FinalTestManifestContract(unittest.TestCase):
    def setUp(self):
        self.template = json.loads(TEMPLATE.read_text())
        self.manifest = json.loads((DATASET / "dataset_manifest.json").read_text())

    def test_required_duration_uses_actual_delivery(self):
        for record in self.manifest["flights"]:
            self.assertIn("duration_s", record)
            self.assertTrue(math.isfinite(float(record["duration_s"])))
            self.assertEqual(record["duration_s"], record["actual_duration_s"])

    def test_no_frozen_template_field_is_omitted(self):
        self.assertTrue(required_paths(self.template) - {"flights"} <= present_paths(self.manifest))
        expected = {row["flight_id"]: row for row in self.template["flights"]}
        actual = {row["flight_id"]: row for row in self.manifest["flights"]}
        self.assertEqual(set(actual), set(expected))
        for fid in expected:
            self.assertFalse(required_paths(expected[fid]) - present_paths(actual[fid]))

    def test_per_flight_truth_contract_is_complete(self):
        root_contract = self.manifest["truth_contract"]
        for record in self.manifest["flights"]:
            self.assertEqual(record["truth_contract"], root_contract)
            self.assertIsInstance(record["truth_contract"]["contract_only_validation_completed"], bool)
            self.assertFalse(record["truth_contract"]["estimator_or_frontend_metrics_computed_before_lock"])

    def test_frozen_template_canonical_values(self):
        for key in ("powerline_qualification_schema_version", "dataset", "dataset_role",
                    "held_out", "flat_layout", "bag_sha256_convention", "split_policy"):
            self.assertEqual(self.manifest[key], self.template[key])
        expected = {row["flight_id"]: row for row in self.template["flights"]}
        for record in self.manifest["flights"]:
            frozen = expected[record["flight_id"]]
            for key in ("flight_id", "split", "seed", "trajectory_type", "bag_path", "scientific_role"):
                self.assertEqual(record[key], frozen[key])
            self.assertNotIn(record["bag_sha256"], {"", "REQUIRED"})
            self.assertNotIn(record["trajectory_definition_sha256"], {"", "REQUIRED"})


if __name__ == "__main__":
    unittest.main()
