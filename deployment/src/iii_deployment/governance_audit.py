"""Read-only comparison of declared and live GitHub governance policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Protocol, Sequence

from .contracts import ContractError, content_identity


OWNER = "DIII-SDU-Group"
WORKSPACE_REPOSITORY = "III-Drone-ros2-ws"
EDITABLE_REPOSITORIES = (
    "III-Drone-Configuration",
    "III-Drone-Contracts",
    "III-Drone-Core",
    "III-Drone-GC",
    "III-Drone-Interfaces",
    "III-Drone-Mission",
    "III-Drone-Runtime",
    "III-Drone-Simulation",
    "III-Drone-Supervision",
    "III-Drone-CLI",
)


class GovernanceClient(Protocol):
    def branches(self, repository: str) -> Sequence[str]: ...

    def ruleset_summaries(self, repository: str) -> Sequence[Mapping[str, Any]]: ...

    def ruleset(self, repository: str, ruleset_id: int) -> Mapping[str, Any]: ...


class GitHubAuditError(RuntimeError):
    pass


class GhClient:
    """Minimal read-only GitHub CLI adapter."""

    def __init__(self, *, owner: str = OWNER) -> None:
        self.owner = owner

    def _get(self, endpoint: str, *, paginate: bool = False) -> Any:
        command = ["gh", "api", "--method", "GET"]
        if paginate:
            command.extend(("--paginate", "--slurp"))
        command.append(endpoint)
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode:
            raise GitHubAuditError(process.stderr.strip() or process.stdout.strip())
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubAuditError(f"GitHub returned invalid JSON for {endpoint}") from exc
        if paginate:
            if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
                raise GitHubAuditError(f"GitHub returned invalid paginated data for {endpoint}")
            return [item for page in value for item in page]
        return value

    def branches(self, repository: str) -> Sequence[str]:
        values = self._get(
            f"repos/{self.owner}/{repository}/branches?per_page=100", paginate=True
        )
        return tuple(item["name"] for item in values)

    def ruleset_summaries(self, repository: str) -> Sequence[Mapping[str, Any]]:
        return self._get(
            f"repos/{self.owner}/{repository}/rulesets?includes_parents=false&per_page=100",
            paginate=True,
        )

    def ruleset(self, repository: str, ruleset_id: int) -> Mapping[str, Any]:
        return self._get(f"repos/{self.owner}/{repository}/rulesets/{ruleset_id}")


@dataclass(frozen=True)
class Finding:
    id: str
    repository: str
    subject: str
    detail: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "repository": self.repository,
            "subject": self.subject,
            "detail": self.detail,
            "remediation": self.remediation,
        }


def canonical_ruleset(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")
    try:
        return {key: value[key] for key in keys}
    except KeyError as exc:
        raise ContractError(f"ruleset is missing required field {exc.args[0]!r}") from exc


def _load_ruleset(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load desired ruleset {path}: {exc}") from exc
    return canonical_ruleset(value)


def desired_governance(root: Path) -> dict[str, dict[str, Any]]:
    ruleset_root = root / "deployment" / "governance" / "rulesets"
    workspace_paths = sorted(ruleset_root.glob("workspace-*.json"))
    submodule_paths = sorted(ruleset_root.glob("submodule-*.json"))
    workspace_values = [_load_ruleset(path) for path in workspace_paths]
    submodule_values = [_load_ruleset(path) for path in submodule_paths]
    workspace_rulesets = {value["name"]: value for value in workspace_values}
    submodule_rulesets = {value["name"]: value for value in submodule_values}
    desired = {
        WORKSPACE_REPOSITORY: {
            "branches": ("develop", "main", "release"),
            "rulesets": workspace_rulesets,
        }
    }
    for repository in EDITABLE_REPOSITORIES:
        desired[repository] = {
            "branches": ("develop", "main"),
            "rulesets": submodule_rulesets,
        }
    return desired


def _required_checks(ruleset: Mapping[str, Any]) -> tuple[str, ...]:
    for rule in ruleset["rules"]:
        if rule["type"] == "required_status_checks":
            return tuple(
                item["context"] for item in rule["parameters"]["required_status_checks"]
            )
    return ()


def _rules_without_checks(ruleset: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [rule for rule in ruleset["rules"] if rule["type"] != "required_status_checks"]


def _audit_ruleset(
    repository: str,
    desired: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    name = str(desired["name"])
    reconcile = "Run: python scripts/governance/manage_github_rulesets.py --apply"

    def drift(identifier: str, field: str, expected: Any, observed: Any) -> None:
        if observed != expected:
            findings.append(
                Finding(
                    identifier,
                    repository,
                    name,
                    f"{field} expected {expected!r}, observed {observed!r}",
                    reconcile,
                )
            )

    drift("RULESET_TARGET_DRIFT", "target", desired["target"], actual.get("target"))
    drift(
        "RULESET_ENFORCEMENT_DRIFT",
        "enforcement",
        desired["enforcement"],
        actual.get("enforcement"),
    )
    drift("RULESET_SCOPE_DRIFT", "conditions", desired["conditions"], actual.get("conditions"))
    drift(
        "BYPASS_ACTOR_DRIFT",
        "bypass actors",
        desired["bypass_actors"],
        actual.get("bypass_actors"),
    )
    expected_checks = _required_checks(desired)
    actual_checks = _required_checks(actual) if isinstance(actual.get("rules"), list) else ()
    drift("REQUIRED_CHECKS_DRIFT", "required checks", expected_checks, actual_checks)
    expected_sources = tuple(check for check in expected_checks if check.startswith("promotion-source-"))
    actual_sources = tuple(check for check in actual_checks if check.startswith("promotion-source-"))
    drift("SOURCE_GATE_DRIFT", "target-specific source gates", expected_sources, actual_sources)
    if isinstance(actual.get("rules"), list):
        drift(
            "RULES_DRIFT",
            "non-status rules",
            _rules_without_checks(desired),
            _rules_without_checks(actual),
        )
    if desired["target"] == "tag":
        expected_types = {"deletion", "non_fast_forward"}
        actual_types = {
            rule.get("type") for rule in actual.get("rules", []) if isinstance(rule, dict)
        }
        protected = (
            actual.get("enforcement") == "active"
            and actual.get("conditions") == desired["conditions"]
            and expected_types.issubset(actual_types)
            and actual.get("bypass_actors") == []
        )
        if not protected:
            findings.append(
                Finding(
                    "TAG_PROTECTION_DRIFT",
                    repository,
                    name,
                    "declared release tags are not active, immutable, exactly scoped, and bypass-free",
                    reconcile,
                )
            )
    return findings


def audit_governance(
    root: Path,
    client: GovernanceClient,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    desired = desired_governance(root)
    findings: list[Finding] = []
    observed_rulesets = 0
    expected_rulesets = sum(len(policy["rulesets"]) for policy in desired.values())
    for repository, policy in desired.items():
        branches = set(client.branches(repository))
        for branch in policy["branches"]:
            if branch not in branches:
                findings.append(
                    Finding(
                        "BRANCH_MISSING",
                        repository,
                        branch,
                        f"required protected branch {branch!r} is missing",
                        "Restore the reviewed branch, then run ruleset reconciliation and this audit.",
                    )
                )
        summaries = client.ruleset_summaries(repository)
        by_name = {str(item["name"]): item for item in summaries}
        unexpected = sorted(set(by_name) - set(policy["rulesets"]))
        for name in unexpected:
            findings.append(
                Finding(
                    "UNEXPECTED_RULESET",
                    repository,
                    name,
                    "live ruleset is not declared by repository policy",
                    "Review the ruleset manually; declare it or remove it through an approved change.",
                )
            )
        for name, desired_ruleset in policy["rulesets"].items():
            summary = by_name.get(name)
            if summary is None:
                findings.append(
                    Finding(
                        "RULESET_MISSING",
                        repository,
                        name,
                        "declared ruleset is absent",
                        "Run: python scripts/governance/manage_github_rulesets.py --apply",
                    )
                )
                continue
            actual = canonical_ruleset(client.ruleset(repository, int(summary["id"])))
            observed_rulesets += 1
            findings.extend(_audit_ruleset(repository, desired_ruleset, actual))

    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report: dict[str, Any] = {
        "schema": "iii.github-governance-audit/v1",
        "recorded_at": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "outcome": "passed" if not findings else "failed",
        "policy_sha256": content_identity(desired),
        "repositories": len(desired),
        "expected_rulesets": expected_rulesets,
        "observed_rulesets": observed_rulesets,
        "findings": [finding.to_dict() for finding in findings],
    }
    report["audit_id"] = content_identity(report)
    return report
