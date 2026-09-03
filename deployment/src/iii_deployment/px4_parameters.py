"""Complete, disarmed, backup-first PX4 parameter inventory transactions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import struct
import sys
import time
import threading
from typing import Any, Callable, Mapping, Protocol, Sequence

from .contracts import ContractRegistry, canonical_json, content_identity

HASH = re.compile(r"^[a-f0-9]{64}$")
PARAMETER = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
MAV_TYPES = {5: "UINT32", 6: "INT32", 9: "REAL32"}
MAV_TYPE_IDS = {value: key for key, value in MAV_TYPES.items()}


class PX4ParameterError(RuntimeError):
    """Fail-closed PX4 inventory or transaction error."""


class PX4ApplyError(PX4ParameterError):
    """PX4 write failed; ``result`` records recovery truth."""

    def __init__(self, message: str, result: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.result = dict(result)


class PX4Adapter(Protocol):
    def status(self) -> Mapping[str, Any]: ...

    def pull_all(self) -> Sequence[Mapping[str, Any]]: ...

    def write(
        self, name: str, value: int | float, mav_type: str
    ) -> Mapping[str, Any]: ...

    def wait_parameter_event(self, timeout: float) -> bool: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise PX4ParameterError(f"PX4 state directory is linked: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise PX4ParameterError(f"PX4 state path is not a directory: {path}")
    metadata = path.stat(follow_symlinks=False)
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PX4ParameterError(f"PX4 state directory has another owner: {path}")
    if metadata.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    parent = _private_directory(path.parent)
    if path.is_symlink():
        raise PX4ParameterError(f"PX4 state target is linked: {path}")
    temporary = parent / f".{path.name}.{os.getpid()}.partial"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PX4ParameterError(f"{label} is missing or linked")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PX4ParameterError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PX4ParameterError(f"{label} must be a JSON object")
    return value


def _identity(value: Mapping[str, Any], field: str) -> str:
    return content_identity({key: item for key, item in value.items() if key != field})


def _version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value)
    if match is None:
        raise PX4ParameterError(f"PX4 firmware version is invalid: {value!r}")
    return tuple(int(item) for item in match.groups())


def _compatible(version: str, expression: str) -> bool:
    observed = _version(version)
    for clause in expression.split(","):
        clause = clause.strip()
        match = re.fullmatch(r"(>=|>|<=|<|==)([0-9]+\.[0-9]+\.[0-9]+)", clause)
        if match is None:
            raise PX4ParameterError(f"PX4 firmware range is unsupported: {expression}")
        expected = _version(match.group(2))
        operation = match.group(1)
        if operation == ">=" and not observed >= expected:
            return False
        if operation == ">" and not observed > expected:
            return False
        if operation == "<=" and not observed <= expected:
            return False
        if operation == "<" and not observed < expected:
            return False
        if operation == "==" and observed != expected:
            return False
    return True


def _matches(observed: int | float, expected: int | float, tolerance: float) -> bool:
    return math.isclose(
        float(observed), float(expected), rel_tol=0.0, abs_tol=float(tolerance)
    )


class PX4ParameterStore:
    """Own manifests, complete snapshots, plans, writes, captures, and exports."""

    def __init__(
        self,
        *,
        manifest_paths: Mapping[str, Path],
        state_root: Path,
        schema_root: Path,
        adapter: PX4Adapter,
        now: Callable[[], str] | None = None,
    ) -> None:
        if set(manifest_paths) != {"real", "sim"}:
            raise PX4ParameterError("PX4 store requires exact real and sim manifests")
        self.registry = ContractRegistry(schema_root)
        self.state_root = _private_directory(state_root.expanduser().absolute())
        self.snapshot_root = _private_directory(self.state_root / "snapshots")
        self.plan_root = _private_directory(self.state_root / "plans")
        self.result_root = _private_directory(self.state_root / "results")
        self.capture_root = _private_directory(self.state_root / "captures")
        self.adapter = adapter
        self.now = now or _now
        self.manifest_paths = {
            name: path.absolute() for name, path in manifest_paths.items()
        }
        self.manifests = {
            name: self._load_manifest(path, profile=name)
            for name, path in self.manifest_paths.items()
        }

    def _load_manifest(self, path: Path, *, profile: str) -> dict[str, Any]:
        value = _load_json(path, f"PX4 {profile} parameter manifest")
        self.registry.validate("px4-parameter-manifest", value)
        if value["profile"] != profile:
            raise PX4ParameterError("PX4 parameter manifest profile mismatch")
        if value["manifest_id"] != _identity(value, "manifest_id"):
            raise PX4ParameterError("PX4 parameter manifest identity mismatch")
        parameters = value["parameters"]
        names = [item["name"] for item in parameters]
        if names != sorted(names) or len(names) != len(set(names)):
            raise PX4ParameterError("PX4 manifest parameters must be unique and sorted")
        if value["inventory"]["parameter_count"] != len(parameters):
            raise PX4ParameterError("PX4 manifest inventory count mismatch")
        for item in parameters:
            if item["classification"] == "calibration-identity":
                if item["enforcement"] != "preserve" or item["value"] is not None:
                    raise PX4ParameterError(
                        "PX4 calibration/identity parameters must be preserved"
                    )
            elif item["enforcement"] != "exact" or item["value"] is None:
                raise PX4ParameterError("PX4 expected parameters require exact values")
            if item["mav_type"] != "REAL32" and item["tolerance"] != 0:
                raise PX4ParameterError("integer PX4 parameters cannot use tolerance")
        return value

    def manifest(self, profile: str) -> dict[str, Any]:
        try:
            return self.manifests[profile]
        except KeyError as exc:
            raise PX4ParameterError(f"unsupported PX4 profile: {profile}") from exc

    def _status(self, profile: str) -> dict[str, Any]:
        status = dict(self.adapter.status())
        required = {
            "connected",
            "armed",
            "system_id",
            "component_id",
            "firmware_version",
            "firmware_commit",
        }
        if set(status) != required or not status["connected"]:
            raise PX4ParameterError("PX4 target status is incomplete or disconnected")
        if status["armed"]:
            raise PX4ParameterError("PX4 parameter inventory is forbidden while armed")
        manifest = self.manifest(profile)
        if not _compatible(
            str(status["firmware_version"]), manifest["firmware"]["compatible_range"]
        ):
            raise PX4ParameterError(
                "PX4 firmware is incompatible with release manifest"
            )
        commit = status["firmware_commit"]
        if (
            not isinstance(commit, str)
            or not re.fullmatch(r"[a-f0-9]{10}", commit)
            or not manifest["firmware"]["reference_commit"].startswith(commit)
        ):
            raise PX4ParameterError("PX4 firmware commit differs from release manifest")
        return status

    def pull(
        self,
        profile: str,
        *,
        provenance: str = "mavlink-complete-inventory",
    ) -> dict[str, Any]:
        status = self._status(profile)
        values = [dict(item) for item in self.adapter.pull_all()]
        if not values:
            raise PX4ParameterError("PX4 returned an empty parameter inventory")
        names = [str(item.get("name")) for item in values]
        indexes = [item.get("index") for item in values]
        declared = {item.get("count") for item in values}
        if (
            len(names) != len(set(names))
            or len(indexes) != len(set(indexes))
            or set(indexes) != set(range(len(values)))
            or declared != {len(values)}
        ):
            raise PX4ParameterError("PX4 parameter inventory is partial or duplicated")
        normalized = []
        for index, item in sorted(zip(indexes, values), key=lambda pair: pair[0]):
            name = item.get("name")
            mav_type = item.get("mav_type")
            value = item.get("value")
            if (
                not isinstance(name, str)
                or not PARAMETER.fullmatch(name)
                or mav_type not in MAV_TYPE_IDS
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise PX4ParameterError("PX4 parameter inventory contains invalid data")
            if mav_type != "REAL32" and not isinstance(value, int):
                raise PX4ParameterError("PX4 integer parameter was not decoded exactly")
            normalized.append(
                {"name": name, "mav_type": mav_type, "value": value, "index": index}
            )
        normalized.sort(key=lambda item: item["name"])
        target = {
            "system_id": int(status["system_id"]),
            "component_id": int(status["component_id"]),
            "armed": False,
            "firmware_version": str(status["firmware_version"]),
            "firmware_commit": status["firmware_commit"],
        }
        identity_input = {
            "profile": profile,
            "target": target,
            "parameter_count": len(normalized),
            "parameters": normalized,
        }
        snapshot_id = content_identity(identity_input)
        destination = self.snapshot_root / f"{snapshot_id}.json"
        if destination.exists():
            return self.load_snapshot(snapshot_id)
        snapshot = {
            "schema": "iii.px4-parameter-snapshot/v1",
            "snapshot_id": snapshot_id,
            "captured_at": self.now(),
            "profile": profile,
            "provenance": provenance,
            "target": target,
            "complete": True,
            "parameter_count": len(normalized),
            "parameters": normalized,
        }
        self.registry.validate("px4-parameter-snapshot", snapshot)
        _atomic_json(destination, snapshot)
        return snapshot

    def load_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(snapshot_id):
            raise PX4ParameterError("PX4 snapshot identity is invalid")
        value = _load_json(
            self.snapshot_root / f"{snapshot_id}.json", "PX4 parameter snapshot"
        )
        self.registry.validate("px4-parameter-snapshot", value)
        expected = content_identity(
            {
                "profile": value["profile"],
                "target": value["target"],
                "parameter_count": value["parameter_count"],
                "parameters": value["parameters"],
            }
        )
        if value["snapshot_id"] != snapshot_id or expected != snapshot_id:
            raise PX4ParameterError("PX4 parameter snapshot content changed")
        if value["parameter_count"] != len(value["parameters"]):
            raise PX4ParameterError("PX4 parameter snapshot count changed")
        return value

    def compare(self, profile: str, snapshot_id: str) -> dict[str, Any]:
        manifest = self.manifest(profile)
        snapshot = self.load_snapshot(snapshot_id)
        if snapshot["profile"] != profile:
            raise PX4ParameterError("PX4 snapshot profile mismatch")
        expected = {item["name"]: item for item in manifest["parameters"]}
        observed = {item["name"]: item for item in snapshot["parameters"]}
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        drift = {name: [] for name in ("release-required", "operator-tunable")}
        preserved = []
        for name in sorted(set(expected) & set(observed)):
            requirement = expected[name]
            current = observed[name]
            if requirement["mav_type"] != current["mav_type"]:
                if requirement["classification"] == "calibration-identity":
                    preserved.append(name)
                else:
                    drift[requirement["classification"]].append(
                        {
                            "name": name,
                            "reason": "type",
                            "expected": requirement["mav_type"],
                            "observed": current["mav_type"],
                        }
                    )
            elif requirement["enforcement"] == "preserve":
                preserved.append(name)
            elif not _matches(
                current["value"], requirement["value"], requirement["tolerance"]
            ):
                drift[requirement["classification"]].append(
                    {
                        "name": name,
                        "reason": "value",
                        "expected": requirement["value"],
                        "observed": current["value"],
                    }
                )
        complete = (
            not missing
            and not unexpected
            and len(observed) == manifest["inventory"]["parameter_count"]
        )
        return {
            "schema": "iii.px4-parameter-comparison/v1",
            "profile": profile,
            "manifest_id": manifest["manifest_id"],
            "snapshot_id": snapshot_id,
            "inventory_complete": complete,
            "missing": missing,
            "unexpected": unexpected,
            "drift": drift,
            "preserved_calibration_identity": preserved,
            "required_match": complete and not drift["release-required"],
        }

    def activation_health(self, profile: str) -> dict[str, Any]:
        snapshot = self.pull(profile)
        comparison = self.compare(profile, snapshot["snapshot_id"])
        return {
            "schema": "iii.px4-activation-health/v1",
            "profile": profile,
            "snapshot_id": snapshot["snapshot_id"],
            "manifest_id": comparison["manifest_id"],
            "parameter_manifest_matches": comparison["required_match"],
            "healthy": (
                comparison["required_match"]
                if profile == "real"
                else comparison["inventory_complete"]
            ),
            "comparison": comparison,
            "writes_performed": 0,
        }

    def activation_evidence(self, profile: str, *, release_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(release_id):
            raise PX4ParameterError("PX4 activation release identity is invalid")
        snapshot = dict(
            self.pull(profile, provenance="qgc-forwarded-mavlink-observation")
        )
        # Snapshot content identities deliberately deduplicate equal parameter
        # sets across observations. The retained content may therefore have
        # originated from an earlier explicit pull. This evidence is based on a
        # fresh pull over QGC's forwarded link, so bind that observation source
        # in the evidence copy without rewriting the immutable content record.
        snapshot["provenance"] = "qgc-forwarded-mavlink-observation"
        comparison = self.compare(profile, snapshot["snapshot_id"])
        healthy = (
            comparison["required_match"]
            if profile == "real"
            else comparison["inventory_complete"]
        )
        evidence = {
            "schema": "iii.px4-activation-evidence/v1",
            "evidence_id": "0" * 64,
            "captured_at": self.now(),
            "release_id": release_id,
            "profile": profile,
            "manifest_id": comparison["manifest_id"],
            "snapshot": snapshot,
            "comparison": comparison,
            "healthy": healthy,
            "writes_performed": 0,
        }
        evidence["evidence_id"] = _identity(evidence, "evidence_id")
        self.registry.validate("px4-activation-evidence", evidence)
        return evidence

    def plan(
        self,
        profile: str,
        snapshot_id: str,
        *,
        selected_keys: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        manifest = self.manifest(profile)
        snapshot = self.load_snapshot(snapshot_id)
        comparison = self.compare(profile, snapshot_id)
        if not comparison["inventory_complete"]:
            raise PX4ParameterError("PX4 write planning requires a complete inventory")
        expected = {item["name"]: item for item in manifest["parameters"]}
        observed = {item["name"]: item for item in snapshot["parameters"]}
        differing = {
            name
            for name, item in expected.items()
            if item["enforcement"] == "exact"
            and not _matches(observed[name]["value"], item["value"], item["tolerance"])
        }
        selected = differing if selected_keys is None else set(selected_keys)
        if not selected or not selected <= differing:
            raise PX4ParameterError(
                "PX4 selected keys must be current exact-value drift"
            )
        changes = [
            {
                "name": name,
                "mav_type": expected[name]["mav_type"],
                "old": observed[name]["value"],
                "new": expected[name]["value"],
                "classification": expected[name]["classification"],
                "tolerance": expected[name]["tolerance"],
            }
            for name in sorted(selected)
        ]
        plan = {
            "schema": "iii.px4-parameter-plan/v1",
            "plan_id": "0" * 64,
            "created_at": self.now(),
            "profile": profile,
            "manifest_id": manifest["manifest_id"],
            "snapshot_id": snapshot_id,
            "target": snapshot["target"],
            "changes": changes,
        }
        plan["plan_id"] = _identity(plan, "plan_id")
        self.registry.validate("px4-parameter-plan", plan)
        _atomic_json(self.plan_root / f"{plan['plan_id']}.json", plan)
        return plan

    def load_plan(self, plan_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(plan_id):
            raise PX4ParameterError("PX4 plan identity is invalid")
        plan = _load_json(self.plan_root / f"{plan_id}.json", "PX4 parameter plan")
        self.registry.validate("px4-parameter-plan", plan)
        if plan["plan_id"] != plan_id or _identity(plan, "plan_id") != plan_id:
            raise PX4ParameterError("PX4 parameter plan content changed")
        if self.manifest(plan["profile"])["manifest_id"] != plan["manifest_id"]:
            raise PX4ParameterError("PX4 parameter plan uses a stale manifest")
        return plan

    def _result(self, value: dict[str, Any]) -> dict[str, Any]:
        value["result_id"] = content_identity(
            {key: item for key, item in value.items() if key != "result_id"}
        )
        _atomic_json(self.result_root / f"{value['result_id']}.json", value)
        return value

    def apply(self, plan_id: str, *, confirmed_keys: Sequence[str]) -> dict[str, Any]:
        plan = self.load_plan(plan_id)
        if set(confirmed_keys) != {item["name"] for item in plan["changes"]}:
            raise PX4ParameterError(
                "PX4 apply confirmation must name every planned key"
            )
        backup = self.pull(plan["profile"])
        if (
            backup["snapshot_id"] != plan["snapshot_id"]
            or backup["target"] != plan["target"]
        ):
            raise PX4ParameterError("PX4 state changed after planning; replan required")
        attempted: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        try:
            for change in plan["changes"]:
                attempted.append(change)
                observed = dict(
                    self.adapter.write(
                        change["name"], change["new"], change["mav_type"]
                    )
                )
                if (
                    observed.get("name") != change["name"]
                    or observed.get("mav_type") != change["mav_type"]
                    or not _matches(
                        observed.get("value"), change["new"], change["tolerance"]
                    )
                ):
                    raise PX4ParameterError(f"PX4 readback failed for {change['name']}")
                applied.append(change)
            verified = self.pull(plan["profile"])
            by_name = {item["name"]: item for item in verified["parameters"]}
            if any(
                not _matches(
                    by_name[item["name"]]["value"], item["new"], item["tolerance"]
                )
                for item in plan["changes"]
            ):
                raise PX4ParameterError("PX4 full-inventory verification failed")
        except Exception as exc:
            recovery_errors = []
            for change in reversed(attempted):
                try:
                    restored = self.adapter.write(
                        change["name"], change["old"], change["mav_type"]
                    )
                    if not _matches(
                        restored.get("value"), change["old"], change["tolerance"]
                    ):
                        raise PX4ParameterError("recovery readback mismatch")
                except Exception as recovery_exc:
                    recovery_errors.append(
                        {"name": change["name"], "error": type(recovery_exc).__name__}
                    )
            recovery_snapshot = None
            recovered = False
            try:
                recovery_snapshot = self.pull(plan["profile"])
                recovered = recovery_snapshot["snapshot_id"] == backup["snapshot_id"]
            except Exception as recovery_exc:
                recovery_errors.append(
                    {"name": "complete-inventory", "error": type(recovery_exc).__name__}
                )
            result = self._result(
                {
                    "schema": "iii.px4-parameter-apply-result/v1",
                    "result_id": "0" * 64,
                    "plan_id": plan_id,
                    "outcome": (
                        "recovered"
                        if recovered and not recovery_errors
                        else "divergent"
                    ),
                    "backup_snapshot_id": backup["snapshot_id"],
                    "verified_snapshot_id": (
                        recovery_snapshot["snapshot_id"] if recovery_snapshot else None
                    ),
                    "applied": [item["name"] for item in attempted],
                    "recovery_errors": recovery_errors,
                    "error": type(exc).__name__,
                }
            )
            raise PX4ApplyError(
                "PX4 write failed and recovery "
                + ("completed" if result["outcome"] == "recovered" else "is divergent"),
                result,
            ) from exc
        return self._result(
            {
                "schema": "iii.px4-parameter-apply-result/v1",
                "result_id": "0" * 64,
                "plan_id": plan_id,
                "outcome": "applied",
                "backup_snapshot_id": backup["snapshot_id"],
                "verified_snapshot_id": verified["snapshot_id"],
                "applied": [item["name"] for item in applied],
                "recovery_errors": [],
                "error": None,
            }
        )

    def verify(self, plan_id: str) -> dict[str, Any]:
        plan = self.load_plan(plan_id)
        snapshot = self.pull(plan["profile"])
        values = {item["name"]: item for item in snapshot["parameters"]}
        mismatches = [
            item["name"]
            for item in plan["changes"]
            if item["name"] not in values
            or not _matches(
                values[item["name"]]["value"], item["new"], item["tolerance"]
            )
        ]
        return {
            "schema": "iii.px4-parameter-verify-result/v1",
            "plan_id": plan_id,
            "snapshot_id": snapshot["snapshot_id"],
            "verified": not mismatches,
            "mismatches": mismatches,
        }

    def capture(
        self, snapshot_id: str, *, short_name: str, description: str
    ) -> dict[str, Any]:
        self.load_snapshot(snapshot_id)
        capture = {
            "schema": "iii.px4-parameter-capture/v1",
            "capture_id": "0" * 64,
            "snapshot_id": snapshot_id,
            "short_name": short_name,
            "description": description,
            "created_at": self.now(),
        }
        capture["capture_id"] = _identity(capture, "capture_id")
        self.registry.validate("px4-parameter-capture", capture)
        _atomic_json(self.capture_root / f"{capture['capture_id']}.json", capture)
        return capture

    def load_capture(self, capture_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(capture_id):
            raise PX4ParameterError("PX4 capture identity is invalid")
        value = _load_json(self.capture_root / f"{capture_id}.json", "PX4 capture")
        self.registry.validate("px4-parameter-capture", value)
        if (
            value["capture_id"] != capture_id
            or _identity(value, "capture_id") != capture_id
        ):
            raise PX4ParameterError("PX4 capture content changed")
        self.load_snapshot(value["snapshot_id"])
        return value

    def list_captures(self) -> list[dict[str, Any]]:
        captures = []
        for path in sorted(self.capture_root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise PX4ParameterError("PX4 capture registry contains an unsafe entry")
            captures.append(self.load_capture(path.stem))
        return captures

    def diff_snapshots(self, left_id: str, right_id: str) -> dict[str, Any]:
        left = self.load_snapshot(left_id)
        right = self.load_snapshot(right_id)
        if left["profile"] != right["profile"]:
            raise PX4ParameterError("PX4 snapshot profiles differ")
        left_values = {item["name"]: item for item in left["parameters"]}
        right_values = {item["name"]: item for item in right["parameters"]}
        names = sorted(set(left_values) | set(right_values))
        changes = []
        for name in names:
            before = left_values.get(name)
            after = right_values.get(name)
            if before != after:
                changes.append(
                    {
                        "name": name,
                        "left": before,
                        "right": after,
                    }
                )
        return {
            "schema": "iii.px4-snapshot-diff/v1",
            "profile": left["profile"],
            "left_snapshot_id": left_id,
            "right_snapshot_id": right_id,
            "changes": changes,
        }

    def export_capture(self, capture_id: str, destination: Path) -> dict[str, Any]:
        if destination.exists() or destination.is_symlink():
            raise PX4ParameterError("PX4 capture export destination already exists")
        capture = self.load_capture(capture_id)
        snapshot = self.load_snapshot(capture["snapshot_id"])
        value = {
            "schema": "iii.px4-capture-export/v1",
            "export_id": "0" * 64,
            "capture": capture,
            "snapshot": snapshot,
        }
        value["export_id"] = _identity(value, "export_id")
        _atomic_json(destination, value)
        return value

    def import_capture(self, source: Path) -> dict[str, Any]:
        value = _load_json(source, "PX4 capture export")
        if value.get("schema") != "iii.px4-capture-export/v1" or value.get(
            "export_id"
        ) != _identity(value, "export_id"):
            raise PX4ParameterError("PX4 capture export identity mismatch")
        snapshot = value.get("snapshot")
        capture = value.get("capture")
        if not isinstance(snapshot, dict) or not isinstance(capture, dict):
            raise PX4ParameterError("PX4 capture export payload is malformed")
        self.registry.validate("px4-parameter-snapshot", snapshot)
        self.registry.validate("px4-parameter-capture", capture)
        if snapshot["snapshot_id"] != content_identity(
            {
                "profile": snapshot["profile"],
                "target": snapshot["target"],
                "parameter_count": snapshot["parameter_count"],
                "parameters": snapshot["parameters"],
            }
        ) or capture["capture_id"] != _identity(capture, "capture_id"):
            raise PX4ParameterError("PX4 imported capture content changed")
        snapshot_path = self.snapshot_root / f"{snapshot['snapshot_id']}.json"
        if snapshot_path.exists():
            self.load_snapshot(snapshot["snapshot_id"])
        else:
            imported = dict(snapshot)
            imported["provenance"] = "imported-verified-capture"
            _atomic_json(snapshot_path, imported)
        capture_path = self.capture_root / f"{capture['capture_id']}.json"
        if capture_path.exists():
            self.load_capture(capture["capture_id"])
        else:
            _atomic_json(capture_path, capture)
        return capture

    def promoted_manifest(
        self, capture_id: str, *, accepted_keys: Sequence[str]
    ) -> dict[str, Any]:
        capture = self.load_capture(capture_id)
        snapshot = self.load_snapshot(capture["snapshot_id"])
        manifest = json.loads(json.dumps(self.manifest(snapshot["profile"])))
        values = {item["name"]: item for item in snapshot["parameters"]}
        accepted = set(accepted_keys)
        parameters = {item["name"]: item for item in manifest["parameters"]}
        if (
            not accepted
            or not accepted <= set(parameters)
            or not accepted <= set(values)
        ):
            raise PX4ParameterError(
                "PX4 promotion keys are absent from capture/manifest"
            )
        for name in accepted:
            if parameters[name]["classification"] == "calibration-identity":
                raise PX4ParameterError(
                    "PX4 calibration/identity promotion is forbidden"
                )
            if parameters[name]["mav_type"] != values[name]["mav_type"]:
                raise PX4ParameterError("PX4 promotion changes parameter type")
            parameters[name]["value"] = values[name]["value"]
        manifest["manifest_id"] = _identity(manifest, "manifest_id")
        self.registry.validate("px4-parameter-manifest", manifest)
        return manifest


class PX4ParameterMonitor:
    """Two-second event debounce plus 60-second disarmed full reconciliation."""

    def __init__(
        self,
        store: PX4ParameterStore,
        *,
        profile: str,
        debounce_seconds: float = 2.0,
        reconcile_seconds: float = 60.0,
    ) -> None:
        if debounce_seconds != 2.0 or reconcile_seconds != 60.0:
            raise PX4ParameterError("PX4 monitor cadence is a fixed release contract")
        self.store = store
        self.profile = profile
        self.debounce_seconds = debounce_seconds
        self.reconcile_seconds = reconcile_seconds
        self.pending_at: float | None = None
        self.last_reconcile_at: float | None = None
        self.last_snapshot_id: str | None = None

    def event(self, observed_at: float) -> None:
        status = self.store.adapter.status()
        if status.get("connected") and not status.get("armed"):
            self.pending_at = observed_at + self.debounce_seconds

    def tick(self, observed_at: float) -> dict[str, Any] | None:
        status = self.store.adapter.status()
        if not status.get("connected") or status.get("armed"):
            self.pending_at = None
            return None
        event_due = self.pending_at is not None and observed_at >= self.pending_at
        periodic_due = (
            self.last_reconcile_at is None
            or observed_at - self.last_reconcile_at >= self.reconcile_seconds
        )
        if not event_due and not periodic_due:
            return None
        snapshot = self.store.pull(
            self.profile, provenance="qgc-forwarded-mavlink-observation"
        )
        self.pending_at = None
        self.last_reconcile_at = observed_at
        changed = snapshot["snapshot_id"] != self.last_snapshot_id
        self.last_snapshot_id = snapshot["snapshot_id"]
        return {
            "snapshot": snapshot,
            "changed": changed,
            "provenance": "mavlink-observation",
        }

    def clean_end(self) -> dict[str, Any] | None:
        status = self.store.adapter.status()
        if not status.get("connected") or status.get("armed"):
            return None
        snapshot = self.store.pull(
            self.profile, provenance="qgc-forwarded-mavlink-observation"
        )
        changed = snapshot["snapshot_id"] != self.last_snapshot_id
        self.last_snapshot_id = snapshot["snapshot_id"]
        return {
            "snapshot": snapshot,
            "changed": changed,
            "provenance": "mavlink-observation",
        }


class MavlinkParameterAdapter:
    """pymavlink adapter for an explicitly selected serial or network endpoint."""

    def __init__(
        self,
        endpoint: str = "udpin:127.0.0.1:14551",
        *,
        timeout: float = 30.0,
    ) -> None:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise PX4ParameterError("pymavlink is required for PX4 parameters") from exc
        if not endpoint.startswith(("udpin:", "udp:", "tcp:", "/dev/")):
            raise PX4ParameterError("PX4 MAVLink endpoint scheme is unsupported")
        self.mavutil = mavutil
        self.connection = mavutil.mavlink_connection(
            endpoint,
            source_system=254,
            source_component=190,
            autoreconnect=True,
        )
        self.timeout = timeout
        self._status: dict[str, Any] | None = None

    @staticmethod
    def _decode(value: float, mav_type: int) -> int | float:
        raw = struct.pack(">f", float(value))
        if mav_type == 5:
            return struct.unpack(">I", raw)[0]
        if mav_type == 6:
            return struct.unpack(">i", raw)[0]
        if mav_type == 9:
            return float(value)
        raise PX4ParameterError(f"unsupported PX4 MAV_PARAM_TYPE: {mav_type}")

    @staticmethod
    def _encode(value: int | float, mav_type: int) -> float:
        if mav_type == 5:
            raw = struct.pack(">I", int(value))
        elif mav_type == 6:
            raw = struct.pack(">i", int(value))
        elif mav_type == 9:
            return float(value)
        else:
            raise PX4ParameterError(f"unsupported PX4 MAV_PARAM_TYPE: {mav_type}")
        return struct.unpack(">f", raw)[0]

    @staticmethod
    def _decode_firmware_commit(value: Any) -> str:
        """Decode PX4's five-byte Git prefix from AUTOPILOT_VERSION.

        PX4 places the big-endian Git integer into the byte array through a
        little-endian uint64, masks it to five bytes, then uses the remaining
        three bytes for the vendor version. Reversing the transmitted array and
        retaining five bytes recovers the advertised 40-bit commit prefix.
        """

        raw = bytes(value)
        if len(raw) != 8:
            raise PX4ParameterError("PX4 firmware commit field is malformed")
        return raw[::-1][:5].hex()

    def _heartbeat(self) -> Any:
        message = self.connection.recv_match(
            type="HEARTBEAT", blocking=True, timeout=self.timeout
        )
        if message is None:
            raise PX4ParameterError("PX4 heartbeat timed out")
        return message

    def _firmware(self) -> tuple[str, str | None]:
        command = self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE
        self.connection.mav.command_long_send(
            self.connection.target_system,
            self.connection.target_component,
            command,
            0,
            self.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        message = self.connection.recv_match(
            type="AUTOPILOT_VERSION", blocking=True, timeout=min(self.timeout, 5)
        )
        if message is None:
            raise PX4ParameterError("PX4 firmware identity timed out")
        packed = int(message.flight_sw_version)
        version = (
            f"{(packed >> 24) & 0xff}.{(packed >> 16) & 0xff}.{(packed >> 8) & 0xff}"
        )
        return version, self._decode_firmware_commit(message.flight_custom_version)

    def status(self) -> Mapping[str, Any]:
        heartbeat = self._heartbeat()
        version, commit = self._firmware()
        self._status = {
            "connected": True,
            "armed": bool(
                int(heartbeat.base_mode)
                & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            ),
            "system_id": int(self.connection.target_system),
            "component_id": int(self.connection.target_component),
            "firmware_version": version,
            "firmware_commit": commit,
        }
        return dict(self._status)

    def read_text_file(self, path: str) -> bytes:
        """Read one fixed PX4 SD configuration file through the MAVLink shell."""

        if path not in {"/fs/microsd/net.cfg", "/fs/microsd/etc/extras.txt"}:
            raise PX4ParameterError("PX4 shell read path is not release-owned")
        begin, end = "III_FILE_BEGIN", "III_FILE_END"
        command = f"echo {begin}; cat {path}; echo {end}\n"
        flags = (
            self.mavutil.mavlink.SERIAL_CONTROL_FLAG_EXCLUSIVE
            | self.mavutil.mavlink.SERIAL_CONTROL_FLAG_RESPOND
        )
        self.connection.mav.serial_control_send(10, flags, 0, 0, 1, [10] + [0] * 69)
        for offset in range(0, len(command), 70):
            block = command[offset : offset + 70].encode("ascii")
            self.connection.mav.serial_control_send(
                10,
                flags,
                0,
                0,
                len(block),
                list(block) + [0] * (70 - len(block)),
            )
        deadline = time.monotonic() + min(self.timeout, 8)
        output = bytearray()
        try:
            while time.monotonic() < deadline:
                message = self.connection.recv_match(
                    type="SERIAL_CONTROL", blocking=True, timeout=0.2
                )
                if message is None or int(message.count) == 0:
                    continue
                output.extend(bytes(message.data[: int(message.count)]))
                normalized = bytes(output).replace(b"\r\n", b"\n")
                begin_marker = (begin + "\n").encode()
                if begin_marker in normalized and (
                    "\n" + end
                ).encode() in normalized.split(begin_marker, 1)[1]:
                    break
        finally:
            self.connection.mav.serial_control_send(10, 0, 0, 0, 0, [0] * 70)
        normalized = bytes(output).replace(b"\r\n", b"\n")
        begin_marker, end_marker = (begin + "\n").encode(), ("\n" + end).encode()
        if begin_marker not in normalized or end_marker not in normalized:
            raise PX4ParameterError("PX4 SD configuration read timed out")
        return normalized.split(begin_marker, 1)[1].split(end_marker, 1)[0] + b"\n"

    def pull_all(self) -> Sequence[Mapping[str, Any]]:
        self.connection.mav.param_request_list_send(
            self.connection.target_system, self.connection.target_component
        )
        values: dict[int, dict[str, Any]] = {}
        count: int | None = None
        deadline = time.monotonic() + self.timeout
        last = time.monotonic()
        while time.monotonic() < deadline:
            message = self.connection.recv_match(
                type="PARAM_VALUE", blocking=True, timeout=0.5
            )
            if message is None:
                if count is not None and len(values) == count:
                    break
                if time.monotonic() - last >= 2:
                    self.connection.mav.param_request_list_send(
                        self.connection.target_system, self.connection.target_component
                    )
                    last = time.monotonic()
                continue
            name = message.param_id
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="strict").rstrip("\x00")
            else:
                name = str(name).rstrip("\x00")
            mav_type_id = int(message.param_type)
            if mav_type_id not in MAV_TYPES:
                raise PX4ParameterError(f"unsupported PX4 parameter type for {name}")
            count = int(message.param_count)
            index = int(message.param_index)
            values[index] = {
                "name": name,
                "mav_type": MAV_TYPES[mav_type_id],
                "value": self._decode(float(message.param_value), mav_type_id),
                "index": index,
                "count": count,
            }
            last = time.monotonic()
            if len(values) == count:
                break
        marker = values.pop(65535, None)
        if (
            count is None
            or marker is None
            or marker.get("name") != "_HASH_CHECK"
            or len(values) != count - 1
            or set(values) != set(range(count - 1))
        ):
            raise PX4ParameterError(
                f"PX4 full inventory timed out ({len(values)}/{(count or 1) - 1})"
            )
        for value in values.values():
            value["count"] = count - 1
        return [values[index] for index in range(count - 1)]

    def write(self, name: str, value: int | float, mav_type: str) -> Mapping[str, Any]:
        if not PARAMETER.fullmatch(name) or mav_type not in MAV_TYPE_IDS:
            raise PX4ParameterError("PX4 parameter write input is invalid")
        status = self.status()
        if status["armed"]:
            raise PX4ParameterError("PX4 parameter write is forbidden while armed")
        type_id = MAV_TYPE_IDS[mav_type]
        self.connection.mav.param_set_send(
            self.connection.target_system,
            self.connection.target_component,
            name.encode("ascii"),
            self._encode(value, type_id),
            type_id,
        )
        deadline = time.monotonic() + min(self.timeout, 5)
        while time.monotonic() < deadline:
            message = self.connection.recv_match(
                type="PARAM_VALUE", blocking=True, timeout=0.5
            )
            if message is None:
                continue
            observed_name = message.param_id
            if isinstance(observed_name, bytes):
                observed_name = observed_name.decode("ascii").rstrip("\x00")
            else:
                observed_name = str(observed_name).rstrip("\x00")
            if observed_name == name:
                observed_type = int(message.param_type)
                return {
                    "name": name,
                    "mav_type": MAV_TYPES.get(observed_type, "unknown"),
                    "value": self._decode(float(message.param_value), observed_type),
                }
        raise PX4ParameterError(f"PX4 parameter readback timed out: {name}")

    def wait_parameter_event(self, timeout: float) -> bool:
        message = self.connection.recv_match(
            type="PARAM_VALUE", blocking=True, timeout=max(0.0, timeout)
        )
        return message is not None


def companion_main(argv: Sequence[str] | None = None) -> int:
    """Run the login-scoped QGC-forwarded PX4 mirror companion."""

    parser = argparse.ArgumentParser(description=companion_main.__doc__)
    parser.add_argument(
        "--profile", choices=("real", "sim", "opti_track", "hil"), required=True
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("III_PX4_MAVLINK_ENDPOINT", "udpin:127.0.0.1:14551"),
    )
    parser.add_argument("--manifest-root", type=Path)
    parser.add_argument("--schema-root", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    prefix = Path(os.environ.get("III_DEPLOYMENT_PREFIX", sys.prefix))
    manifest_root = args.manifest_root or prefix / "share/iii-deployment/px4"
    schema_root = args.schema_root or prefix / "share/iii-deployment/schemas/v1"
    state_root = args.state_root or Path(
        os.environ.get(
            "III_PX4_PARAMETER_STATE_ROOT",
            str(Path.home() / ".local/state/iii/registry/px4"),
        )
    )
    parameter_profile = {
        "real": "real",
        "sim": "sim",
        "opti_track": "real",
        "hil": "sim",
    }[args.profile]
    adapter = MavlinkParameterAdapter(args.endpoint)
    store = PX4ParameterStore(
        manifest_paths={
            "real": manifest_root / "real.json",
            "sim": manifest_root / "sim.json",
        },
        state_root=state_root,
        schema_root=schema_root,
        adapter=adapter,
    )
    monitor = PX4ParameterMonitor(store, profile=parameter_profile)
    stopped = threading.Event()

    def stop(_signal: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    status_path = store.state_root / "companion-status.json"
    while not stopped.is_set():
        try:
            if adapter.wait_parameter_event(1.0):
                monitor.event(time.monotonic())
            result = monitor.tick(time.monotonic())
            if result is not None:
                _atomic_json(
                    status_path,
                    {
                        "schema": "iii.px4-parameter-companion-state/v1",
                        "outcome": "reconciled",
                        "profile": parameter_profile,
                        "snapshot_id": result["snapshot"]["snapshot_id"],
                        "changed": result["changed"],
                        "provenance": "mavlink-observation",
                    },
                )
                if args.once:
                    return 0
        except (PX4ParameterError, OSError, ValueError) as exc:
            _atomic_json(
                status_path,
                {
                    "schema": "iii.px4-parameter-companion-state/v1",
                    "outcome": "degraded",
                    "profile": parameter_profile,
                    "error": type(exc).__name__,
                    "writes_performed": 0,
                },
            )
            if args.once:
                return 20
            stopped.wait(2.0)
    try:
        final = monitor.clean_end()
        if final is not None:
            _atomic_json(
                status_path,
                {
                    "schema": "iii.px4-parameter-companion-state/v1",
                    "outcome": "clean-session-end",
                    "profile": parameter_profile,
                    "snapshot_id": final["snapshot"]["snapshot_id"],
                    "changed": final["changed"],
                    "provenance": "mavlink-observation",
                },
            )
    except (PX4ParameterError, OSError, ValueError):
        return 20
    return 0
