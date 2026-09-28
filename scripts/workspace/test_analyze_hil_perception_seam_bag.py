#!/usr/bin/env python3
"""Deterministic branch tests for the HIL perception-seam classifier."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from scripts.workspace.analyze_hil_perception_seam_bag import (
    PI_RECORDING_CONTRACT,
    WORKSTATION_RECORDING_CONTRACT,
    _apply_recording_contract,
    _pointcloud_point_count,
    _validated_reset_barrier,
    classify_summary,
)


def _stage(topic: str, count: int, **extra):
    value = {"topic": topic, "present": count > 0, "count": count}
    value.update(extra)
    return value


def _summary(**overrides):
    result = {
        "schema": "hil-perception-seam-summary/v1",
        "run_id": "test-run",
        "recording_complete": True,
        "fixture_valid": True,
        "observations": {
            "workstation_source": _stage("/simulation/local/cable_camera/image_raw", 10),
            "workstation_relay": _stage("/sensor/cable_camera/image_raw", 10),
            "pi_camera": _stage("/sensor/cable_camera/image_raw", 10),
            "hough": _stage("/perception/hough_transformer/cable_yaw_angle", 10),
            "direction": _stage("/perception/pl_dir_computer/powerline_direction_quat", 10),
            "mmwave": _stage("/sensor/mmwave/points", 10),
            "mapper": _stage(
                "/perception/pl_mapper/powerline",
                10,
                first_stamp_ns=100,
                last_stamp_ns=200,
                advancing=True,
                max_line_count=3,
            ),
        },
        "tf": {
            "topic": "/tf",
            "present": True,
            "lookup": {"success": True, "samples": 10},
        },
    }
    result.update(overrides)
    return result


class SeamClassifierTests(unittest.TestCase):
    def assertClassification(self, summary, expected):
        result = classify_summary(summary)
        self.assertEqual(result["classification"], expected, result)

    def test_h1_source_stall(self):
        summary = _summary()
        summary["observations"]["workstation_source"] = _stage(
            "/simulation/local/cable_camera/image_raw", 0
        )
        self.assertClassification(summary, "H1 source stall")

    def test_h2_cross_host_relay_loss(self):
        summary = _summary()
        summary["observations"]["pi_camera"] = _stage("/sensor/cable_camera/image_raw", 0)
        self.assertClassification(summary, "H2 cross-host relay loss")

    def test_h3_camera_present_but_no_hough(self):
        summary = _summary()
        summary["observations"]["hough"] = _stage(
            "/perception/hough_transformer/cable_yaw_angle", 0
        )
        self.assertClassification(summary, "H3 camera present but no Hough output")

    def test_h4_hough_present_but_no_direction_or_tf(self):
        summary = _summary()
        summary["observations"]["direction"] = _stage(
            "/perception/pl_dir_computer/powerline_direction_quat", 0
        )
        self.assertClassification(summary, "H4 Hough present but no direction/TF")

    def test_h4_hough_and_direction_present_but_tf_lookup_fails(self):
        summary = _summary()
        summary["tf"]["lookup"]["success"] = False
        self.assertClassification(summary, "H4 Hough present but no direction/TF")

    def test_h5_direction_and_mmwave_but_empty_mapper(self):
        summary = _summary()
        summary["observations"]["mapper"] = _stage(
            "/perception/pl_mapper/powerline",
            10,
            first_stamp_ns=100,
            last_stamp_ns=200,
            advancing=True,
            max_line_count=0,
        )
        self.assertClassification(summary, "H5 direction+mmWave present but mapper cannot form/advance")

    def test_positive_seam(self):
        self.assertClassification(_summary(), "positive seam")

    def test_fixture_invalid(self):
        self.assertClassification(_summary(fixture_valid=False), "inconclusive")

    def test_identity_direction_quaternion_is_traffic(self):
        summary = _summary()
        summary["observations"]["direction"].update(
            type="geometry_msgs/msg/QuaternionStamped",
            quaternions=[{"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}],
            non_identity_count=0,
        )
        self.assertClassification(summary, "positive seam")

    def test_missing_fixture_evidence_is_inconclusive(self):
        summary = _summary()
        del summary["fixture_valid"]
        self.assertClassification(summary, "inconclusive")

    def test_missing_mapper_ack_is_recording_incomplete(self):
        summary = _summary(recording_complete=False)
        summary["recording_quality"] = {"mapper_ack_success": False}
        self.assertClassification(summary, "recording-incomplete")

    def test_pointcloud_point_count(self):
        self.assertEqual(_pointcloud_point_count(SimpleNamespace(width=3, height=2)), 6)
        self.assertEqual(_pointcloud_point_count(SimpleNamespace(width=0, height=9)), 0)

    def test_dual_host_barrier_requires_ack_and_both_receipt_timestamps(self):
        barrier = {
            "schema": "hil-perception-seam-reset-barrier/v2",
            "run_id": "test-run",
            "mapper_service": "/perception/pl_mapper/pl_mapper_command",
            "request": {"command": 0, "reset": True},
            "mapper_ack": 0,
            "workstation_bag_timestamp_ns": 100,
            "pi_bag_timestamp_ns": 200,
        }
        self.assertEqual(_validated_reset_barrier(barrier, "test-run"), barrier)
        barrier["mapper_ack"] = 1
        self.assertIsNone(_validated_reset_barrier(barrier, "test-run"))
        barrier["mapper_ack"] = 0
        del barrier["pi_bag_timestamp_ns"]
        self.assertIsNone(_validated_reset_barrier(barrier, "test-run"))

    def test_recording_contract_allows_zero_messages_for_advertised_support_topics(self):
        for contract in (WORKSTATION_RECORDING_CONTRACT, PI_RECORDING_CONTRACT):
            stages, complete = _apply_recording_contract({}, dict(contract), contract)
            self.assertTrue(complete)
            self.assertTrue(
                all(stage["count"] == 0 for stage in stages.values())
            )
            self.assertTrue(
                all(stage["message_count_observed"] for stage in stages.values())
            )

    def test_recording_contract_rejects_missing_or_wrong_supporting_topic_type(self):
        advertised = dict(PI_RECORDING_CONTRACT)
        del advertised["/perception/pl_dir_computer/status"]
        _, complete = _apply_recording_contract({}, advertised, PI_RECORDING_CONTRACT)
        self.assertFalse(complete)

        advertised = dict(PI_RECORDING_CONTRACT)
        advertised["/perception/pl_dir_computer/status"] = "std_msgs/msg/String"
        _, complete = _apply_recording_contract({}, advertised, PI_RECORDING_CONTRACT)
        self.assertFalse(complete)

    def test_recording_incomplete(self):
        self.assertClassification(_summary(recording_complete=False), "recording-incomplete")

    def test_missing_data_is_retained_as_negative_evidence(self):
        summary = _summary()
        summary["observations"]["hough"] = {"topic": "/perception/hough_transformer/cable_yaw_angle"}
        result = classify_summary(summary)
        self.assertEqual(result["classification"], "H3 camera present but no Hough output")
        self.assertIn(
            {"stage": "hough", "topic": "/perception/hough_transformer/cable_yaw_angle", "present": None, "count": None},
            result["negative_evidence"],
        )

    def test_contradiction_defaults_to_inconclusive(self):
        summary = _summary()
        summary["observations"]["workstation_source"]["active"] = False
        self.assertClassification(summary, "inconclusive")
        result = classify_summary(summary)
        self.assertTrue(result["contradictions"])

    def test_input_is_not_mutated(self):
        summary = _summary()
        original = copy.deepcopy(summary)
        classify_summary(summary)
        self.assertEqual(summary, original)


if __name__ == "__main__":
    unittest.main()
