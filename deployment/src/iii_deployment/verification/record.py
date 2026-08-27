"""Create one signed, artifact-bound local verification evidence record."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
from typing import Iterable

from iii_deployment.contracts import ContractRegistry, canonical_json

from .evidence import build_evidence, sign_evidence, validate_evidence
from .matrix import load_policy, read_matrix
from .storage import write_bytes_exclusive_atomic


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pairs(values: Iterable[str], *, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item or key in result:
            raise ValueError(f"{label} must contain unique ROW=VALUE entries")
        result[key] = item
    return result


def record(
    arguments: list[str] | None = None, *, expected_level: str | None = None
) -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate-set", required=True, type=Path)
    parser.add_argument("--result", required=True, action="append")
    parser.add_argument("--reason", action="append", default=[])
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--environment", action="append", default=[])
    parser.add_argument("--impact-category", action="append", required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--started-at", required=True)
    args = parser.parse_args(arguments)

    root = args.root.resolve()
    matrix = read_matrix(root / "deployment/verification/matrix.json")
    policy = load_policy(root / "deployment/verification/policy.json")
    candidate = json.loads(args.candidate_set.read_text(encoding="utf-8"))
    results = _pairs(args.result, label="--result")
    reasons = _pairs(args.reason, label="--reason")
    raw_artifacts = _pairs(args.artifact, label="--artifact")
    environment = {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        **_pairs(args.environment, label="--environment"),
    }
    definitions = {row["id"]: row for row in matrix["rows"]}
    levels = {definitions[row]["level"] for row in results if row in definitions}
    if len(levels) != 1 or any(row not in definitions for row in results):
        raise ValueError("results must select known rows from exactly one matrix level")
    level = levels.pop()
    if expected_level is not None and level != expected_level:
        raise ValueError(f"this runner accepts only {expected_level} matrix rows")
    rows = []
    output_parent = args.output.resolve().parent
    for identifier, status in results.items():
        artifacts = []
        if identifier in raw_artifacts:
            path = Path(raw_artifacts[identifier]).resolve()
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{identifier}: artifact is missing or linked")
            try:
                relative = path.relative_to(output_parent)
            except ValueError as exc:
                raise ValueError(
                    f"{identifier}: artifacts must reside under the output directory"
                ) from exc
            artifacts.append(
                {
                    "path": relative.as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        rows.append(
            {
                "id": identifier,
                "status": status,
                "reason": reasons.get(identifier),
                "evidence": artifacts,
            }
        )
    value = build_evidence(
        matrix=matrix,
        policy=policy,
        level=level,
        candidate_set=candidate,
        started_at=args.started_at,
        finished_at=_utc_now(),
        environment=environment,
        impact_categories=args.impact_category,
        rows=rows,
    )
    value = sign_evidence(value, args.signing_key)
    validate_evidence(
        value,
        matrix=matrix,
        policy=policy,
        registry=ContractRegistry(root / "deployment/schemas/v1"),
        require_signature=False,
    )
    try:
        write_bytes_exclusive_atomic(args.output, canonical_json(value) + b"\n")
    except FileExistsError as exc:
        raise ValueError("refusing to replace an existing evidence record") from exc
    return value


def main(
    arguments: list[str] | None = None, *, expected_level: str | None = None
) -> int:
    value = record(arguments, expected_level=expected_level)
    print(json.dumps({"record_id": value["record_id"], "rows": len(value["rows"])}))
    return 0
