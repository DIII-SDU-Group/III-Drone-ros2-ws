#!/usr/bin/env python3
"""Render the HIL perception-seam report without evaluating report content."""

from __future__ import annotations

import argparse
from pathlib import Path


EVIDENCE_LINE = (
    "See `seam_summary.json`, `seam_analysis.json`, `topic_manifest.json`, "
    "`workstation_bag`, `pi_recording/pi_bag`, `topic_info/`, both host "
    "recordings, topic/node/service inventories, TF probe, mapper service "
    "response, process snapshots, and `SHA256SUMS.txt`."
)


def _custody_text(*, phase: str, status: str) -> str:
    if phase == "initial":
        return (
            "This report records creation state only. It does not assert runtime "
            "startup, mapper reset, or observation."
        )
    if status == "REFUSED":
        return (
            "The probe stopped at a refusal boundary. This report does not assert "
            "runtime startup, mapper reset, or observation."
        )
    return (
        "Runtime actions, if any, were limited to probe-owned canonical paths under "
        "the stated custody policy. See the recorded evidence for observed state."
    )


def render_report(
    *,
    output: Path,
    run_id: str,
    status: str,
    reason: str,
    classification: str,
    phase: str,
) -> None:
    classification = classification or "not-run"
    if status != "SUCCESS":
        scope = (
            "The intended probe is one stationary, non-flight observation with fixed "
            "5s pre-roll and 45s after exactly one PL mapper START+RESET service "
            "call. Manifest and event records identify the actions actually reached; "
            "this report does not assert that reset or observation completed. No arm, "
            "takeoff, mission, custom operation, maneuver, or PX4 vehicle command is "
            "asserted by this report."
        )
    else:
        scope = (
            "One stationary, non-flight observation with 5s pre-roll and 45s after "
            "exactly one PL mapper START+RESET service call. No arm, takeoff, "
            "mission, custom operation, maneuver, or PX4 vehicle command was issued."
        )

    reason_line = f"\nReason: {reason}" if reason else ""
    if phase == "initial":
        transition_text = (
            "The run has been created; all refusal, failure, and completion "
            "transitions are recorded in `manifest.json` and `events.jsonl`."
        )
    else:
        transition_text = (
            "Refusal, failure, and completion transitions are recorded in "
            "`manifest.json` and `events.jsonl`."
        )

    report = (
        f"# HIL Perception Seam Probe — {run_id}\n\n"
        f"Tag: `[HIL-SEAM-{run_id}]`\n\n"
        "## Scope\n\n"
        f"{scope}\n\n"
        "## Status\n\n"
        f"`{status}`{reason_line}\n\n"
        "## Classification\n\n"
        f"`{classification}`\n\n"
        "The analyzer preserves missing topics and contradictions in "
        "`seam_analysis.json`.\n\n"
        "## Runtime custody\n\n"
        f"{_custody_text(phase=phase, status=status)}\n\n"
        "## Evidence\n\n"
        f"{EVIDENCE_LINE}\n\n"
        f"{transition_text}\n"
    )
    output.write_text(report, encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--classification", default="not-run")
    parser.add_argument("--phase", choices=("initial", "exit", "final"), required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    render_report(
        output=args.output,
        run_id=args.run_id,
        status=args.status,
        reason=args.reason,
        classification=args.classification,
        phase=args.phase,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
