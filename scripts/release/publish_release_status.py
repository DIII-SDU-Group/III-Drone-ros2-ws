#!/usr/bin/env python3
"""Append and publish one protected signed release-status transition."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json  # noqa: E402
from iii_deployment.github_publication import (  # noqa: E402
    GhReleasePublisher, STATUS_INDEX_NAME, STATUS_STATEMENT_NAME, publish_release_status,
)
from iii_deployment.qualified_release import PUBLICATION_NAME, verify_release_publication, write_canonical  # noqa: E402
from iii_deployment.release_registry import GitHubReleaseSource  # noqa: E402
from iii_deployment.release_status import append_status, verify_status_index  # noqa: E402
from iii_deployment.signers import load_trusted_signers  # noqa: E402


REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")


def _canonical(data: bytes, label: str) -> dict:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is not JSON") from exc
    if not isinstance(value, dict) or data != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def _release_rows(repository: str) -> list[dict]:
    process = subprocess.run(
        [
            "gh", "api", "--method", "GET", "--paginate", "--slurp",
            f"repos/{repository}/releases?per_page=100",
        ],
        capture_output=True, text=True, check=False,
    )
    if process.returncode:
        raise ContractError(process.stderr.strip() or "cannot list release-status records")
    try:
        pages = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("GitHub returned invalid release-status inventory") from exc
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ContractError("GitHub release-status inventory is not paginated lists")
    return [
        {"tagName": row.get("tag_name"), "isDraft": row.get("draft")}
        for page in pages for row in page if isinstance(row, dict)
    ]


def _latest_index(source: GitHubReleaseSource, repository: str) -> dict | None:
    status_rows = [
        row for row in _release_rows(repository)
        if not row.get("isDraft") and str(row.get("tagName", "")).startswith("iii-status-")
    ]
    if not status_rows:
        return None
    sequences = []
    for row in status_rows:
        suffix = str(row["tagName"]).removeprefix("iii-status-")
        if not suffix.isdigit() or int(suffix) < 1:
            raise ContractError("published release-status tag has an invalid sequence")
        sequences.append((int(suffix), row["tagName"]))
    if len({sequence for sequence, _tag in sequences}) != len(sequences):
        raise ContractError("published release-status sequence is ambiguous")
    tag = max(sequences)[1]
    return _canonical(source.read_asset(tag, STATUS_INDEX_NAME), "release-status index")


def _release_branch_commit() -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if process.returncode or len(process.stdout.strip()) != 40:
        raise ContractError("cannot resolve trusted release-branch workflow commit")
    return process.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--status", choices=("qualified", "withdrawn", "unsafe"), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--superseding-version")
    parser.add_argument("--expected-statement-id")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--initial", action="store_true")
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--status-trust", type=Path, required=True)
    parser.add_argument("--bundle-trust", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.initial and args.status != "qualified":
            raise ContractError("initial release status must be qualified")
        source = GitHubReleaseSource(args.repository)
        publication = _canonical(
            source.read_asset(args.version, PUBLICATION_NAME), "release publication"
        )
        bundle_trust = load_trusted_signers(args.bundle_trust, REGISTRY)
        status_trust = load_trusted_signers(args.status_trust, REGISTRY)
        verify_release_publication(publication, bundle_trust, REGISTRY)
        if publication["version"] != args.version:
            raise ContractError("release publication differs from requested status version")
        previous = _latest_index(source, args.repository)
        latest = verify_status_index(previous, status_trust, REGISTRY) if previous else {}
        current = latest.get(publication["release_id"])
        if args.initial and current is not None:
            if current["version"] == args.version and current["status"] == "qualified":
                print(json.dumps({"schema": "iii.release-status-publication-result/v1", "outcome": "passed", "publication": "no-op", "statement_id": current["statement_id"]}, sort_keys=True))
                return 0
            raise ContractError("initial status already exists with a conflicting state")
        expected = args.expected_statement_id or None
        update = append_status(
            previous,
            operation_id=args.operation_id,
            release_id=publication["release_id"],
            version=args.version,
            status=args.status,
            reason=args.reason,
            superseding_version=args.superseding_version,
            expected_statement_id=expected,
            recorded_at=args.recorded_at,
            private_key_path=args.private_key,
            trusted_signers=status_trust,
            registry=REGISTRY,
        )
        if update is None:
            current_id = current["statement_id"] if current else None
            print(json.dumps({"schema": "iii.release-status-publication-result/v1", "outcome": "passed", "publication": "no-op", "statement_id": current_id}, sort_keys=True))
            return 0
        statement, index = update
        # Re-authenticate the global predecessor immediately before mutation.
        refreshed = _latest_index(source, args.repository)
        before_id = None if previous is None else previous["index_id"]
        refreshed_id = None if refreshed is None else refreshed["index_id"]
        if refreshed_id != before_id:
            raise ContractError("release-status index changed after sequence allocation")
        args.output.mkdir(parents=True, exist_ok=False)
        statement_path = args.output / STATUS_STATEMENT_NAME
        index_path = args.output / STATUS_INDEX_NAME
        write_canonical(statement_path, statement)
        write_canonical(index_path, index)
        outcome = publish_release_status(
            GhReleasePublisher(args.repository),
            statement_path=statement_path,
            index_path=index_path,
            target_commit=_release_branch_commit(),
            trusted_signers=status_trust,
            registry=REGISTRY,
        )
        print(json.dumps({"schema": "iii.release-status-publication-result/v1", "outcome": "passed", "publication": outcome, "statement_id": statement["statement_id"], "sequence": statement["sequence"]}, sort_keys=True))
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "iii.release-status-publication-result/v1", "outcome": "rejected", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
