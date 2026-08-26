"""Stock-preserving Raspberry Pi boot profile inspection and drift evaluation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import stat
from typing import Any, Mapping

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)

PROFILE_SCHEMA = "iii.boot-profile/v1"
INSPECTION_SCHEMA = "iii.boot-inspection/v1"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_INCLUDE_DEPTH = 8


def _under(root: Path, absolute: str) -> Path:
    path = Path(absolute)
    if not path.is_absolute():
        raise ContractError("boot profile path is not absolute")
    return root.joinpath(*path.parts[1:])


def _canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def validate_boot_profile(
    value: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    profile = dict(value)
    registry.validate("boot-profile", profile)
    if profile["profile_id"] != content_identity(
        {key: item for key, item in profile.items() if key != "profile_id"}
    ):
        raise ContractError("boot profile identity mismatch")
    managed = profile["firmware"]["managed_settings"]
    forbidden = set(profile["firmware"]["forbidden_settings"])
    if forbidden.intersection(managed):
        raise ContractError("boot profile manages a forbidden tuning setting")
    return profile


def load_boot_profile(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    return validate_boot_profile(
        _canonical_object(path, label="boot profile"), registry
    )


def _read_bounded(path: Path, *, binary: bool = False) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"boot evidence is missing or linked: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read boot evidence: {exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ContractError("boot evidence exceeds the fixed inspection limit")
    if not binary:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError("boot text is not UTF-8") from exc
    return raw


def _config_directives(
    root: Path,
    path: Path,
    *,
    active_sections: set[str],
    section: str = "global",
    depth: int = 0,
    observed: set[Path] | None = None,
) -> list[dict[str, Any]]:
    if depth > MAX_INCLUDE_DEPTH:
        raise ContractError("boot config include depth exceeds policy")
    allowed_root = _under(root, "/boot/firmware").resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"boot config include is unavailable: {exc}") from exc
    if not resolved.is_relative_to(allowed_root):
        raise ContractError("boot config include escapes /boot/firmware")
    seen = observed if observed is not None else set()
    if resolved in seen:
        raise ContractError("boot config include cycle detected")
    seen.add(resolved)
    raw = _read_bounded(resolved)
    directives: list[dict[str, Any]] = []
    current = section
    for number, original in enumerate(raw.decode("utf-8").splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            candidate = line[1:-1].strip().lower()
            if not candidate or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in candidate
            ):
                raise ContractError("boot config section is malformed")
            current = candidate
            continue
        if line.lower().startswith("include "):
            key, separator, item = "include", " ", line.split(None, 1)[1]
        else:
            key, separator, item = line.partition("=")
        key = key.strip()
        item = item.strip()
        if not separator or not key or len(item) > 512:
            raise ContractError("boot config directive is malformed")
        if key.lower() == "include":
            include = Path(item)
            if include.is_absolute() or any(
                part in {"", ".", ".."} for part in include.parts
            ):
                raise ContractError("boot config include path is unsafe")
            directives.extend(
                _config_directives(
                    root,
                    resolved.parent / include,
                    active_sections=active_sections,
                    section=current,
                    depth=depth + 1,
                    observed=seen,
                )
            )
            continue
        if any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
            for character in key
        ):
            raise ContractError("boot config key is malformed")
        directives.append(
            {
                "source": "/" + str(resolved.relative_to(root.resolve())),
                "line": number,
                "section": current,
                "key": key.lower(),
                "value": item,
                "active": current in active_sections,
            }
        )
    seen.remove(resolved)
    return directives


def _command_line(raw: bytes, sensitive: set[str]) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ContractError("kernel command line is not UTF-8") from exc
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ContractError("kernel command line has invalid control characters")
    tokens: list[dict[str, Any]] = []
    for token in text.split():
        key, separator, item = token.partition("=")
        if not key or len(key) > 128 or len(item) > 512:
            raise ContractError("kernel command-line token is malformed")
        redacted = key in sensitive
        tokens.append(
            {
                "key": key,
                "value": "<redacted>" if redacted else item if separator else None,
                "redacted": redacted,
            }
        )
    return tokens


def inspect_boot(
    profile: Mapping[str, Any],
    *,
    root: Path = Path("/"),
    kernel_release: str | None = None,
    kernel_version: str | None = None,
    architecture: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    paths = profile["paths"]
    drift: list[str] = []
    config_path = _under(root, paths["config"])
    config_sha = None
    config_mode = None
    directives: list[dict[str, Any]] = []
    try:
        config_raw = _read_bounded(config_path)
        config_sha = hashlib.sha256(config_raw).hexdigest()
        config_mode = (
            f"{stat.S_IMODE(config_path.stat(follow_symlinks=False).st_mode):04o}"
        )
        directives = _config_directives(
            root,
            config_path,
            active_sections=set(profile["firmware"]["active_sections"]),
        )
        if config_path.stat(follow_symlinks=False).st_mode & 0o022:
            drift.append("firmware config is writable outside root")
    except ContractError as exc:
        drift.append(str(exc))
    active = [item for item in directives if item["active"]]
    by_key: dict[str, list[str]] = {}
    overlays: list[str] = []
    for item in active:
        by_key.setdefault(item["key"], []).append(item["value"])
        if item["key"] == "dtoverlay":
            overlays.append(item["value"].split(",", 1)[0])
    for key, expected in profile["firmware"]["managed_settings"].items():
        if not by_key.get(key) or by_key[key][-1] != expected:
            drift.append(f"managed firmware setting {key} differs")
    for key in profile["firmware"]["forbidden_settings"]:
        if key in by_key:
            drift.append(f"forbidden firmware setting {key} is active")
    missing_overlays = sorted(
        set(profile["firmware"]["managed_overlays"]) - set(overlays)
    )
    if missing_overlays:
        drift.append(
            "required device-tree overlays are absent: " + ", ".join(missing_overlays)
        )

    cmdline_path = _under(root, paths["cmdline"])
    command_sha = None
    command_tokens: list[dict[str, Any]] = []
    raw_command = b""
    try:
        raw_command = _read_bounded(cmdline_path)
        command_sha = hashlib.sha256(raw_command).hexdigest()
        command_tokens = _command_line(
            raw_command, set(profile["kernel"]["sensitive_value_keys"])
        )
    except ContractError as exc:
        drift.append(str(exc))
    raw_tokens = raw_command.decode("utf-8", errors="ignore").strip().split()
    for required in profile["kernel"]["required_command_line_tokens"]:
        if required not in raw_tokens:
            drift.append(f"required kernel command-line token is absent: {required}")
    for forbidden in profile["kernel"]["forbidden_command_line_tokens"]:
        if forbidden in raw_tokens:
            drift.append(f"forbidden kernel command-line token is active: {forbidden}")

    def optional_binary(path: str) -> bytes | None:
        try:
            return _read_bounded(_under(root, path), binary=True)
        except ContractError:
            return None

    model_raw = optional_binary(paths["model"])
    revision_raw = optional_binary(paths["revision"])
    model = (
        model_raw.rstrip(b"\x00").decode("utf-8", errors="replace")[:256]
        if model_raw
        else None
    )
    revision = revision_raw.hex() if revision_raw else None
    required_model = profile["firmware"]["required_model_substring"]
    if model is None or required_model not in model:
        drift.append("device-tree model is not the declared Raspberry Pi 5")
    actual_architecture = architecture or platform.machine()
    if actual_architecture != profile["kernel"]["architecture"]:
        drift.append("kernel architecture differs from the boot profile")
    boot_id_raw = (
        _read_bounded(_under(root, "/proc/sys/kernel/random/boot_id"))
        .decode("ascii")
        .strip()
    )
    value: dict[str, Any] = {
        "schema": INSPECTION_SCHEMA,
        "inspection_id": "0" * 64,
        "profile_id": profile["profile_id"],
        "target_class": profile["target_class"],
        "boot_id": boot_id_raw,
        "accepted": not drift,
        "kernel": {
            "release": kernel_release or platform.release(),
            "version": kernel_version or platform.version(),
            "architecture": actual_architecture,
        },
        "firmware": {
            "model": model,
            "revision_hex": revision,
            "config_sha256": config_sha,
            "config_mode": config_mode,
            "directives": directives,
        },
        "command_line": {"sha256": command_sha, "tokens": command_tokens},
        "drift": sorted(set(drift)),
    }
    value["inspection_id"] = content_identity(
        {key: item for key, item in value.items() if key != "inspection_id"}
    )
    return value


class BootInspector:
    def __init__(
        self,
        *,
        profile_path: Path,
        registry: ContractRegistry,
        root: Path = Path("/"),
    ) -> None:
        self.profile_path = profile_path
        self.registry = registry
        self.root = root

    def inspect(self) -> dict[str, Any]:
        profile = load_boot_profile(self.profile_path, self.registry)
        report = inspect_boot(profile, root=self.root)
        self.registry.validate("boot-inspection", report)
        return report
