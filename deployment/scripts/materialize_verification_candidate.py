#!/usr/bin/env python3
"""Create the exact clean candidate identity shared by every Q131 row."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import SEMVER, canonical_json, content_identity
from iii_deployment.verification.matrix import load_policy, read_matrix
from iii_deployment.verification.storage import write_bytes_exclusive_atomic


def _git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise ValueError(process.stderr.strip() or "Git inspection failed")
    return process.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if not __import__("re").fullmatch(r"[0-9a-f]{64}", args.release_id):
        raise ValueError("--release-id must be a SHA-256 identity")
    if not SEMVER.fullmatch(args.release_version):
        raise ValueError("--release-version must be strict vX.Y.Z SemVer")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("verification candidates require an entirely clean workspace")
    workspace_commit = _git(root, "rev-parse", "HEAD")
    lock = root / "deps/submodule-lock.txt"
    manifest_path = root / "deployment/documentation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = load_policy(root / "deployment/verification/policy.json")
    matrix = read_matrix(root / "deployment/verification/matrix.json")
    if matrix["policy_id"] != content_identity(policy):
        raise ValueError("verification matrix and policy identities differ")
    body = {
        "workspace_commit": workspace_commit,
        "submodule_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "release_id": args.release_id,
        "release_version": args.release_version,
        "documentation_manifest_id": content_identity(manifest),
        "verification_policy_id": matrix["policy_id"],
    }
    candidate = {**body, "candidate_set_id": content_identity(body)}
    try:
        write_bytes_exclusive_atomic(args.output, canonical_json(candidate) + b"\n")
    except FileExistsError as exc:
        raise ValueError("refusing to replace an existing candidate set") from exc
    print(candidate["candidate_set_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
