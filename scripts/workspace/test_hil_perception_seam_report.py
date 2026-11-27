#!/usr/bin/env python3
"""Offline tests for the HIL perception-seam report renderer."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RENDERER = Path(__file__).with_name("hil_perception_seam_report.py")


class HilPerceptionSeamReportTests(unittest.TestCase):
    def _render(self, *, phase: str, status: str) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            output = temporary_path / "REPORT.md"
            sentinel = temporary_path / "SHOULD_NOT_EXIST"
            hostile = f"`$(touch {sentinel})`"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RENDERER),
                    "--output",
                    str(output),
                    "--run-id",
                    f"run{hostile}",
                    "--status",
                    status,
                    "--reason",
                    f"reason{hostile}",
                    "--classification",
                    f"classification{hostile}",
                    "--phase",
                    phase,
                ],
                check=True,
            )
            self.assertFalse(sentinel.exists())
            report = output.read_text(encoding="utf-8")
            self.assertIn(hostile, report)
            return report

    def test_initial_report_is_complete_and_does_not_claim_runtime(self):
        report = self._render(phase="initial", status="IN_PROGRESS")
        self.assertIn("Tag: `[HIL-SEAM-run`$(touch ", report)
        self.assertIn("`IN_PROGRESS`", report)
        self.assertIn("classification`$(touch ", report)
        self.assertIn("`seam_summary.json`", report)
        self.assertIn("does not assert runtime startup, mapper reset, or observation", report)

    def test_refused_report_does_not_claim_runtime(self):
        report = self._render(phase="exit", status="REFUSED")
        self.assertIn("`REFUSED`", report)
        self.assertIn("does not assert runtime startup, mapper reset, or observation", report)

    def test_initial_and_failed_reports_use_prospective_exact_scope(self):
        for phase, status in (("initial", "IN_PROGRESS"), ("final", "FAILED")):
            with self.subTest(phase=phase, status=status):
                report = self._render(phase=phase, status=status)
                self.assertIn(
                    "The intended probe is one stationary, non-flight observation",
                    report,
                )
                self.assertIn("after exactly one PL mapper START+RESET service call", report)
                self.assertIn("does not assert that reset or observation completed", report)
                self.assertNotIn("after at most one", report)

    def test_leading_hyphen_argv_values_are_preserved_as_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "REPORT.md"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RENDERER),
                    f"--output={output}",
                    "--run-id=-run-id",
                    "--status=-FAILED",
                    "--reason=-reason",
                    "--classification=-classification",
                    "--phase=final",
                ],
                check=True,
            )
            report = output.read_text(encoding="utf-8")
            self.assertIn("-run-id", report)
            self.assertIn("`-FAILED`", report)
            self.assertIn("Reason: -reason", report)
            self.assertIn("`-classification`", report)

    def test_failed_and_success_reports_preserve_hostile_text(self):
        for status in ("FAILED", "SUCCESS"):
            with self.subTest(status=status):
                report = self._render(phase="final", status=status)
                self.assertIn(f"`{status}`", report)
                self.assertIn("reason`$(touch ", report)
                self.assertIn("classification`$(touch ", report)
                self.assertIn("`SHA256SUMS.txt`", report)


if __name__ == "__main__":
    unittest.main()
