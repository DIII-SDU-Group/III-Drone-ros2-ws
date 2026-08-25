#!/usr/bin/env python3
"""Verify untrusted linked-PR locators against trusted base and GitHub API state."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.contracts import ContractError  # noqa: E402
from iii_deployment.pr_metadata import (  # noqa: E402
    trusted_submodule_repositories,
    validate_pr_marker_claims,
)


def _gh(endpoint: str, *, paginate: bool = False) -> Any:
    command = ["gh", "api", "--method", "GET"]
    if paginate:
        command.extend(("--paginate", "--slurp"))
    command.append(endpoint)
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode:
        raise ContractError(process.stderr.strip() or process.stdout.strip())
    value = json.loads(process.stdout)
    if paginate:
        return [item for page in value for item in page]
    return value


def _write_summary(report: dict[str, Any]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "<!-- iii-linked-submodule-pr-verification-v1 -->",
        f"### Linked III Submodule PR Gate: {report['outcome'].upper()}",
        "",
        "| Submodule | Verified API URL | Merged | Base |",
        "|---|---|---:|---|",
    ]
    lines.extend(
        f"| `{row['path']}` | {row['url']} | {row['merged']} | `{row['base']}` |"
        for row in report["rows"]
    )
    if not report["rows"]:
        lines.append("| _No changed III gitlinks_ | — | — | — |")
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "")))
    parser.add_argument("--gitmodules", type=Path, default=ROOT / ".gitmodules")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        pull = event["pull_request"]
        owner = event["repository"]["owner"]["login"]
        repository = event["repository"]["name"]
        number = int(event["number"])
        base = pull["base"]["ref"]
        files = _gh(f"repos/{owner}/{repository}/pulls/{number}/files?per_page=100", paginate=True)
        changed_paths = [item["filename"] for item in files]
        trusted = trusted_submodule_repositories(args.gitmodules.read_text(encoding="utf-8"))
        claims = validate_pr_marker_claims(
            pull.get("body") or "",
            changed_paths=changed_paths,
            trusted_repositories=trusted,
        )
        rows = []
        failures = []
        for claim in claims:
            data = _gh(f"repos/{claim.repository}/pulls/{claim.number}")
            merged = bool(data.get("merged_at"))
            observed_base = data.get("base", {}).get("ref")
            observed_url = data.get("html_url")
            valid = merged and observed_base == base and observed_url == claim.url
            rows.append(
                {
                    "path": claim.path,
                    "url": claim.url,
                    "merged": merged,
                    "base": observed_base,
                    "verified": valid,
                }
            )
            if not valid:
                failures.append(
                    f"{claim.path}: API state does not prove {claim.url} merged into {base}"
                )
        report = {
            "schema": "iii.linked-submodule-pr-verification/v1",
            "outcome": "passed" if not failures else "failed",
            "base": base,
            "rows": rows,
            "failures": failures,
        }
        _write_summary(report)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0 if not failures else 20
    except (ContractError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "schema": "iii.linked-submodule-pr-verification/v1",
                    "outcome": "rejected",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
