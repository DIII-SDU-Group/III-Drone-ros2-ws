#!/usr/bin/env python3
"""Plan/apply exact repository rulesets through authenticated GitHub CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.governance_audit import EDITABLE_REPOSITORIES, OWNER  # noqa: E402


class GitHubError(RuntimeError):
    pass


def gh(method: str, endpoint: str, *, value: dict[str, Any] | None = None) -> Any:
    command = ["gh", "api", "--method", method, endpoint]
    temporary: Path | None = None
    try:
        if value is not None:
            handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
            temporary = Path(handle.name)
            json.dump(value, handle, sort_keys=True)
            handle.close()
            command.extend(("--input", str(temporary)))
        process = subprocess.run(command, capture_output=True, text=True, check=False)
        if process.returncode:
            raise GitHubError(process.stderr.strip() or process.stdout.strip())
        return json.loads(process.stdout) if process.stdout.strip() else None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def canonical_ruleset(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")}


def ensure_release_branch(*, apply: bool) -> dict[str, Any]:
    branches = gh("GET", f"repos/{OWNER}/III-Drone-ros2-ws/branches?per_page=100")
    if any(branch["name"] == "release" for branch in branches):
        return {"action": "none", "branch": "release"}
    main = gh("GET", f"repos/{OWNER}/III-Drone-ros2-ws/git/ref/heads/main")
    result = {"action": "create", "branch": "release", "sha": main["object"]["sha"]}
    if apply:
        gh("POST", f"repos/{OWNER}/III-Drone-ros2-ws/git/refs", value={"ref": "refs/heads/release", "sha": main["object"]["sha"]})
    return result


def reconcile(repo: str, desired_paths: list[Path], *, apply: bool) -> list[dict[str, Any]]:
    summaries = gh("GET", f"repos/{OWNER}/{repo}/rulesets?includes_parents=false")
    existing_by_name = {item["name"]: item for item in summaries}
    results: list[dict[str, Any]] = []
    for path in desired_paths:
        desired = json.loads(path.read_text(encoding="utf-8"))
        existing_summary = existing_by_name.get(desired["name"])
        if existing_summary is None:
            action = "create"
            if apply:
                response = gh("POST", f"repos/{OWNER}/{repo}/rulesets", value=desired)
                ruleset_id = response["id"]
            else:
                ruleset_id = None
        else:
            ruleset_id = existing_summary["id"]
            current = canonical_ruleset(gh("GET", f"repos/{OWNER}/{repo}/rulesets/{ruleset_id}"))
            action = "none" if current == desired else "update"
            if apply and action == "update":
                gh("PUT", f"repos/{OWNER}/{repo}/rulesets/{ruleset_id}", value=desired)
        results.append({"repository": repo, "ruleset": desired["name"], "action": action, "id": ruleset_id})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the plan; default is read-only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        paths = sorted((ROOT / "deployment/governance/rulesets").glob("workspace-*.json"))
        submodule_paths = sorted((ROOT / "deployment/governance/rulesets").glob("submodule-*.json"))
        rulesets = reconcile("III-Drone-ros2-ws", paths, apply=args.apply)
        for repository in EDITABLE_REPOSITORIES:
            rulesets.extend(reconcile(repository, submodule_paths, apply=args.apply))
        plan = {
            "schema": "iii.github-ruleset-operation/v1",
            "mode": "apply" if args.apply else "plan",
            "release_branch": ensure_release_branch(apply=args.apply),
            "rulesets": rulesets,
            "next_actions": [{
                "command": ["python", "scripts/governance/audit_github_rulesets.py", "--json"],
                "reason": "Verify live branch and ruleset enforcement after reconciliation.",
                "mutating": False
            }],
        }
        if args.json:
            print(json.dumps(plan, sort_keys=True))
        else:
            for row in plan["rulesets"]:
                print(f"{row['action'].upper()}: {row['repository']} / {row['ruleset']}")
            print("Next: python scripts/governance/audit_github_rulesets.py --json")
        return 0
    except (GitHubError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "iii.github-ruleset-operation/v1", "outcome": "failed", "error": str(exc)}) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
