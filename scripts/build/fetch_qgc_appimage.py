#!/usr/bin/env python3
"""Fetch the exact policy-pinned QGroundControl AppImage without mutable aliases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractError, ContractRegistry  # noqa: E402


def _load_policy(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load QGroundControl policy: {exc}") from exc
    ContractRegistry(ROOT / "deployment/schemas/v1").validate(
        "gc-application-policy", value
    )
    return value


def fetch(policy_path: Path, output: Path) -> dict:
    policy = _load_policy(policy_path)["qgroundcontrol"]
    if output.exists() or output.is_symlink():
        raise ContractError("QGroundControl output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.parent / f".{output.name}.partial-{os.getpid()}"
    if partial.exists() or partial.is_symlink():
        raise ContractError("QGroundControl partial output already exists")
    digest = hashlib.sha256()
    observed = 0
    request = Request(
        policy["source_url"],
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "iii-qualified-release/1",
        },
    )
    try:
        with urlopen(request, timeout=60) as response, partial.open("xb") as stream:
            final_host = response.url.split("/", 3)[2].lower()
            if final_host not in {
                "github.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            }:
                raise ContractError(
                    "QGroundControl download redirected outside the official GitHub asset hosts"
                )
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                observed += len(block)
                if observed > policy["bytes"]:
                    raise ContractError(
                        "QGroundControl download exceeds its pinned size"
                    )
                digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if observed != policy["bytes"] or digest.hexdigest() != policy["sha256"]:
            raise ContractError(
                "QGroundControl download differs from its pinned size/checksum"
            )
        partial.chmod(0o555)
        os.replace(partial, output)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ContractError(
            f"cannot fetch pinned QGroundControl AppImage: {exc}"
        ) from exc
    finally:
        if partial.exists() and not partial.is_symlink():
            partial.unlink()
    return {
        "schema": "iii.qgc-fetch-result/v1",
        "version": policy["version"],
        "path": str(output),
        "sha256": policy["sha256"],
        "bytes": policy["bytes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy", type=Path, default=ROOT / "deployment/gc-application-policy.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = fetch(args.policy, args.output)
    except ContractError as exc:
        value = {
            "schema": "iii.qgc-fetch-result/v1",
            "outcome": "failed",
            "error": str(exc),
        }
        print(json.dumps(value, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30
    value = {**result, "outcome": "passed"}
    print(
        json.dumps(value, sort_keys=True) if args.json else f"PASS: {result['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
