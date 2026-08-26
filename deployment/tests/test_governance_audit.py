from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from iii_deployment.governance_audit import audit_governance, desired_governance


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self) -> None:
        desired = desired_governance(ROOT)
        self.branch_values = {
            repository: list(policy["branches"]) for repository, policy in desired.items()
        }
        self.summary_values: dict[str, list[dict[str, Any]]] = {}
        self.ruleset_values: dict[tuple[str, int], dict[str, Any]] = {}
        ruleset_id = 1000
        for repository, policy in desired.items():
            self.summary_values[repository] = []
            for name, value in policy["rulesets"].items():
                ruleset_id += 1
                self.summary_values[repository].append({"id": ruleset_id, "name": name})
                self.ruleset_values[(repository, ruleset_id)] = deepcopy(value)

    def branches(self, repository: str) -> Sequence[str]:
        return tuple(self.branch_values[repository])

    def ruleset_summaries(self, repository: str) -> Sequence[Mapping[str, Any]]:
        return tuple(self.summary_values[repository])

    def ruleset(self, repository: str, ruleset_id: int) -> Mapping[str, Any]:
        return deepcopy(self.ruleset_values[(repository, ruleset_id)])

    def ruleset_by_name(self, repository: str, name: str) -> dict[str, Any]:
        summary = next(item for item in self.summary_values[repository] if item["name"] == name)
        return self.ruleset_values[(repository, summary["id"])]


def test_exact_live_governance_produces_concise_retained_evidence() -> None:
    report = audit_governance(ROOT, FakeClient(), now=NOW)
    assert report["outcome"] == "passed"
    assert report["repositories"] == 11
    assert report["expected_rulesets"] == report["observed_rulesets"] == 25
    assert report["findings"] == []
    assert len(report["audit_id"]) == 64
    assert len(json.dumps(report, sort_keys=True)) < 1000


def test_audit_reports_missing_branch_ruleset_and_unexpected_ruleset() -> None:
    client = FakeClient()
    client.branch_values["III-Drone-ros2-ws"].remove("release")
    client.summary_values["III-Drone-CLI"] = [
        item for item in client.summary_values["III-Drone-CLI"] if item["name"] != "Main protection"
    ]
    client.summary_values["III-Drone-Core"].append({"id": 99999, "name": "Undeclared"})
    report = audit_governance(ROOT, client, now=NOW)
    assert report["outcome"] == "failed"
    assert {finding["id"] for finding in report["findings"]} >= {
        "BRANCH_MISSING",
        "RULESET_MISSING",
        "UNEXPECTED_RULESET",
    }


def test_audit_reports_required_check_source_gate_and_bypass_drift() -> None:
    client = FakeClient()
    ruleset = client.ruleset_by_name("III-Drone-ros2-ws", "Main protection")
    ruleset["bypass_actors"] = [{"actor_id": 1, "actor_type": "Team", "bypass_mode": "always"}]
    status_rule = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
    status_rule["parameters"]["required_status_checks"] = [
        item
        for item in status_rule["parameters"]["required_status_checks"]
        if item["context"] != "promotion-source-main"
    ]
    report = audit_governance(ROOT, client, now=NOW)
    identifiers = {finding["id"] for finding in report["findings"]}
    assert "BYPASS_ACTOR_DRIFT" in identifiers
    assert "REQUIRED_CHECKS_DRIFT" in identifiers
    assert "SOURCE_GATE_DRIFT" in identifiers


def test_audit_reports_qualified_tag_protection_drift() -> None:
    client = FakeClient()
    ruleset = client.ruleset_by_name("III-Drone-ros2-ws", "Qualified tag protection")
    ruleset["enforcement"] = "disabled"
    ruleset["rules"] = [{"type": "deletion"}]
    report = audit_governance(ROOT, client, now=NOW)
    identifiers = {finding["id"] for finding in report["findings"]}
    assert "TAG_PROTECTION_DRIFT" in identifiers
    assert "RULESET_ENFORCEMENT_DRIFT" in identifiers
    assert "RULES_DRIFT" in identifiers
