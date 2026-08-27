#!/usr/bin/env python3
"""Exercise real -> OptiTrack -> real against one already-deployed release."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable


SCHEMA = "iii.pre-field-profile-matrix/v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _find(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                found.append(item)
            found.extend(_find(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find(item, key))
    return found


class MatrixRun:
    def __init__(self, executable: str, root: Path, expected_release_id: str):
        self.executable = executable
        self.root = root
        self.expected_release_id = expected_release_id
        self.rows: list[dict[str, Any]] = []

    def invoke(
        self,
        name: str,
        arguments: Iterable[str],
        *,
        require_profile: str | None = None,
        require_release: bool = False,
    ) -> dict[str, Any]:
        argv = [self.executable, *arguments, "--output=json"]
        started = _utc_now()
        process = subprocess.run(argv, check=False, capture_output=True)
        finished = _utc_now()
        log = self.root / f"{len(self.rows) + 1:02d}-{name}.log"
        _atomic(
            log,
            b"argv="
            + _canonical(argv)
            + b"\nstdout:\n"
            + process.stdout
            + b"\nstderr:\n"
            + process.stderr,
        )
        try:
            result = json.loads(process.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{name}: III result is not valid JSON") from exc
        row = {
            "name": name,
            "argv": argv,
            "started_at": started,
            "finished_at": finished,
            "process_exit_code": process.returncode,
            "result_code": result.get("code"),
            "outcome": result.get("outcome"),
            "log": log.name,
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        }
        self.rows.append(row)
        if result.get("schema") != "iii.command-result/v1":
            raise RuntimeError(f"{name}: unsupported III result schema")
        if process.returncode != 0 or result.get("outcome") != "success":
            raise RuntimeError(
                f"{name}: command failed with {result.get('outcome')} / {result.get('code')}"
            )
        if require_profile is not None:
            profiles = {str(value) for value in _find(result, "profile") if value}
            if require_profile not in profiles:
                raise RuntimeError(
                    f"{name}: authenticated result does not prove profile {require_profile}"
                )
        if require_release:
            releases = {
                str(value)
                for key in ("release_id", "active_release_id")
                for value in _find(result, key)
                if value
            }
            if self.expected_release_id not in releases:
                raise RuntimeError(
                    f"{name}: authenticated result does not prove the expected release"
                )
        return result


def _plan(
    executable: str, expected_release_id: str, output_dir: Path
) -> dict[str, Any]:
    commands = [
        [executable, "field", "check", "--target", "real", "--output=json"],
        [executable, "system", "stop", "--output=json"],
        [executable, "system", "boot", "--profile", "opti_track", "--output=json"],
        [executable, "system", "status", "--output=json"],
        [executable, "system", "stop", "--output=json"],
        [executable, "system", "boot", "--profile", "real", "--output=json"],
        [executable, "field", "check", "--target", "real", "--output=json"],
    ]
    body = {
        "schema": "iii.pre-field-profile-matrix-plan/v1",
        "expected_release_id": expected_release_id,
        "output_dir": str(output_dir),
        "mutations": commands[1:6],
        "commands": commands,
        "recovery": commands[4:6],
    }
    return {**body, "plan_id": hashlib.sha256(_canonical(body)).hexdigest()}


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iii", default="iii")
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(arguments)
    if not __import__("re").fullmatch(r"[0-9a-f]{64}", args.expected_release_id):
        raise ValueError("--expected-release-id must be a SHA-256 identity")
    plan = _plan(args.iii, args.expected_release_id, args.output_dir)
    if not args.apply:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    root = args.output_dir.resolve()
    if root.exists() or root.is_symlink():
        raise ValueError("refusing to replace a pre-field evidence directory")
    root.mkdir(parents=True, mode=0o700)
    _atomic(root / "plan.json", _canonical(plan) + b"\n")
    _atomic(
        root / "state.json",
        _canonical({"state": "in_progress", "plan_id": plan["plan_id"]}) + b"\n",
    )
    run = MatrixRun(args.iii, root, args.expected_release_id)
    failure: str | None = None
    switched = False
    recovered = False
    started = _utc_now()
    try:
        run.invoke(
            "real-readiness-before",
            ["field", "check", "--target", "real"],
            require_profile="real",
            require_release=True,
        )
        run.invoke("real-stop", ["system", "stop"])
        switched = True
        run.invoke(
            "opti-track-boot",
            ["system", "boot", "--profile", "opti_track"],
            require_profile="opti_track",
        )
        run.invoke(
            "opti-track-status",
            ["system", "status"],
            require_profile="opti_track",
            require_release=True,
        )
    except Exception as exc:
        failure = str(exc)
    finally:
        if switched:
            try:
                run.invoke("return-stop", ["system", "stop"])
                run.invoke(
                    "return-real-boot",
                    ["system", "boot", "--profile", "real"],
                    require_profile="real",
                )
                recovered = True
            except Exception as exc:
                failure = f"{failure + '; ' if failure else ''}real-profile recovery failed: {exc}"
    if recovered:
        try:
            run.invoke(
                "real-readiness-after",
                ["field", "check", "--target", "real"],
                require_profile="real",
                require_release=True,
            )
        except Exception as exc:
            failure = f"{failure + '; ' if failure else ''}{exc}"
    report_body = {
        "schema": SCHEMA,
        "plan_id": plan["plan_id"],
        "expected_release_id": args.expected_release_id,
        "started_at": started,
        "finished_at": _utc_now(),
        "status": "pass" if failure is None else "fail",
        "failure": failure,
        "returned_to_real": recovered,
        "steps": run.rows,
    }
    report = {
        **report_body,
        "report_id": hashlib.sha256(_canonical(report_body)).hexdigest(),
    }
    _atomic(root / "report.json", _canonical(report) + b"\n")
    _atomic(
        root / "state.json",
        _canonical(
            {
                "state": "complete" if failure is None else "failed",
                "plan_id": plan["plan_id"],
                "report_id": report["report_id"],
            }
        )
        + b"\n",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if failure is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
