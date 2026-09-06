"""Root-owned transactional operator networking with fail-closed rollback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_identity
from .receiver.state import atomic_bytes, atomic_document, read_boot_id

INPUT_SCHEMA = "iii.operator-network-input/v1"
PLAN_SCHEMA = "iii.network-plan/v1"
TRANSACTION_SCHEMA = "iii.network-transaction/v1"
STATUS_SCHEMA = "iii.network-status/v1"
CONFIRMATION_DEADLINE_S = 90
NETPLAN_PATH = Path("/etc/netplan/90-iii-operator.yaml")
STATE_ROOT = Path("/var/lib/iii/deployment/network")


class NetworkError(ContractError):
    """A candidate profile or transactional mutation failed closed."""


def load_network_input(path: Path) -> dict[str, Any]:
    """Load an owner-only, ignored/non-repository input without echoing secrets."""

    if path.is_symlink() or not path.is_file():
        raise NetworkError("network input must be a real regular file")
    metadata = path.stat(follow_symlinks=False)
    allowed_uids = {os.geteuid()} if hasattr(os, "geteuid") else set()
    sudo_uid = os.environ.get("SUDO_UID")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and sudo_uid:
        try:
            allowed_uids.add(int(sudo_uid))
        except ValueError as exc:
            raise NetworkError("SUDO_UID is not a valid local user identity") from exc
    if hasattr(os, "geteuid") and metadata.st_uid not in allowed_uids:
        raise NetworkError(
            "network input must be owned by the current or invoking sudo user"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise NetworkError(
            "network input permissions must be owner-only (0600 or stricter)"
        )
    repository = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if repository.returncode == 0:
        root = Path(repository.stdout.strip()).resolve()
        try:
            relative = path.resolve().relative_to(root)
        except ValueError as exc:
            raise NetworkError(
                "network input repository containment is inconsistent"
            ) from exc
        ignored = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(relative)],
            check=False,
            capture_output=True,
            text=True,
        )
        if ignored.returncode == 1:
            raise NetworkError("network input inside a Git worktree must be ignored")
        if ignored.returncode != 0:
            raise NetworkError("cannot verify Git-ignore status for network input")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NetworkError(f"cannot read network input: {exc}") from exc
    if not isinstance(value, dict):
        raise NetworkError("network input must contain one JSON object")
    validate_network_input(value)
    return value


def validate_network_input(value: Mapping[str, Any]) -> None:
    if set(value) != {"schema", "ethernet_dhcp4", "wifi"}:
        raise NetworkError("network input fields do not match the fixed contract")
    if value.get("schema") != INPUT_SCHEMA or value.get("ethernet_dhcp4") is not True:
        raise NetworkError("network input must preserve operator USB Ethernet DHCP")
    wifi = value.get("wifi")
    if not isinstance(wifi, list) or len(wifi) > 16:
        raise NetworkError("network input must contain zero to sixteen Wi-Fi profiles")
    ssids: set[str] = set()
    for row in wifi:
        if not isinstance(row, dict) or not {"ssid", "password"} <= set(row) <= {
            "ssid",
            "password",
            "hidden",
        }:
            raise NetworkError("Wi-Fi profile fields do not match the fixed contract")
        ssid = row.get("ssid")
        password = row.get("password")
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
            raise NetworkError("Wi-Fi SSID must contain one to thirty-two UTF-8 bytes")
        if ssid in ssids:
            raise NetworkError("network input contains duplicate Wi-Fi SSIDs")
        ssids.add(ssid)
        if not isinstance(password, str) or not 8 <= len(password) <= 63:
            raise NetworkError(
                "Wi-Fi password must contain eight to sixty-three characters"
            )
        if "\n" in password or "\r" in password:
            raise NetworkError("Wi-Fi password cannot contain line breaks")
        if "hidden" in row and not isinstance(row["hidden"], bool):
            raise NetworkError("Wi-Fi hidden flag must be boolean")


def render_netplan(value: Mapping[str, Any]) -> bytes:
    """Render JSON, a strict YAML subset, so credentials cannot escape quoting."""

    validate_network_input(value)
    network: dict[str, Any] = {
        "network": {
            "version": 2,
            "ethernets": {
                "operator-usb-ethernet": {
                    "match": {"name": "enx*"},
                    "dhcp4": True,
                    "optional": True,
                }
            },
        }
    }
    if value["wifi"]:
        access_points: dict[str, Any] = {}
        for row in value["wifi"]:
            settings: dict[str, Any] = {"password": row["password"]}
            if row.get("hidden") is True:
                settings["hidden"] = True
            access_points[row["ssid"]] = settings
        network["network"]["wifis"] = {
            "wlan0": {
                "dhcp4": True,
                "optional": True,
                "access-points": access_points,
            }
        }
    return json.dumps(network, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def redacted_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_network_input(value)
    return {
        "ethernet_dhcp4": True,
        "wifi_profile_ids": sorted(
            hashlib.sha256(row["ssid"].encode("utf-8")).hexdigest()
            for row in value["wifi"]
        ),
        "wifi_profile_count": len(value["wifi"]),
        "onboard_access_point": False,
    }


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise NetworkError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetworkError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise NetworkError(f"{label} is not canonical JSON")
    return value


class NetworkController:
    def __init__(
        self,
        *,
        root: Path = Path("/"),
        monotonic_ns: Callable[[], int],
        boot_id: Callable[[], str] = read_boot_id,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        maintenance_safe: Callable[[], bool] = lambda: False,
        stop_runtime: Callable[[], Any] = lambda: None,
        resume_runtime: Callable[[], Any] = lambda: None,
    ) -> None:
        self.root = root.absolute()
        self.netplan_path = self.root / NETPLAN_PATH.relative_to("/")
        self.state_root = self.root / STATE_ROOT.relative_to("/")
        self.monotonic_ns = monotonic_ns
        self.boot_id = boot_id
        self.run = run
        self.maintenance_safe = maintenance_safe
        self.stop_runtime = stop_runtime
        self.resume_runtime = resume_runtime

    def _transaction_root(self, operation_id: str) -> Path:
        if not operation_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in operation_id
        ):
            raise NetworkError("network operation ID is invalid")
        return self.state_root / "transactions" / operation_id

    def _transaction_path(self, operation_id: str) -> Path:
        return self._transaction_root(operation_id) / "transaction.json"

    def plan(
        self,
        *,
        operation_id: str,
        client_id: str,
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        desired = render_netplan(profile)
        prior = (
            self.netplan_path.read_bytes()
            if self.netplan_path.is_file() and not self.netplan_path.is_symlink()
            else None
        )
        if self.netplan_path.is_symlink():
            raise NetworkError("installed operator Netplan configuration is linked")
        value: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "network_id": "",
            "operation_id": operation_id,
            "client_id": client_id,
            "candidate_sha256": hashlib.sha256(canonical_json(profile)).hexdigest(),
            "desired_netplan_sha256": hashlib.sha256(desired).hexdigest(),
            "previous_netplan_sha256": (
                None if prior is None else hashlib.sha256(prior).hexdigest()
            ),
            "profile": redacted_profile(profile),
            "connectivity_impacting": prior != desired,
            "confirmation_deadline_s": CONFIRMATION_DEADLINE_S,
            "no_change": prior == desired,
            "declared_permissions": [
                "/etc/netplan/90-iii-operator.yaml",
                "/var/lib/iii/deployment/network",
                "iii-network-revert@.timer",
            ],
            "required_checks": [
                "operator USB Ethernet DHCP remains enabled",
                "netplan generate succeeds before apply",
                "onboard monotonic rollback timer is armed before detachment",
            ],
        }
        value["network_id"] = content_identity(
            {key: item for key, item in value.items() if key != "network_id"}
        )
        return value

    def claim(self, plan: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
        expected = self.plan(
            operation_id=str(plan["operation_id"]),
            client_id=str(plan["client_id"]),
            profile=profile,
        )
        if dict(plan) != expected:
            raise NetworkError(
                "network profile differs from the retained redacted plan"
            )
        directory = self._transaction_root(str(plan["operation_id"]))
        if directory.exists() or directory.is_symlink():
            raise NetworkError("network operation already has claimed input")
        directory.mkdir(parents=True, mode=0o700)
        atomic_bytes(directory / "desired.netplan", render_netplan(profile), mode=0o600)
        transaction = {
            "schema": TRANSACTION_SCHEMA,
            "operation_id": plan["operation_id"],
            "network_id": plan["network_id"],
            "client_id": plan["client_id"],
            "state": "claimed",
            "accepted_boot_id": self.boot_id(),
            "accepted_monotonic_ns": self.monotonic_ns(),
            "deadline_monotonic_ns": None,
            "previous_present": None,
            "previous_mode": None,
            "failure": None,
            "plan": dict(plan),
        }
        atomic_document(self._transaction_path(str(plan["operation_id"])), transaction)

    def _run_checked(self, argv: Sequence[str]) -> None:
        try:
            self.run(list(argv), check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise NetworkError(
                f"network command failed: {' '.join(argv)}: {exc}"
            ) from exc

    def _save_transaction(self, value: Mapping[str, Any]) -> None:
        atomic_document(self._transaction_path(str(value["operation_id"])), value)

    def _restore(self, transaction: dict[str, Any]) -> None:
        directory = self._transaction_root(transaction["operation_id"])
        backup = directory / "previous.netplan"
        if transaction["previous_present"]:
            if backup.is_symlink() or not backup.is_file():
                raise NetworkError("network rollback backup is missing or linked")
            atomic_bytes(
                self.netplan_path,
                backup.read_bytes(),
                mode=int(transaction["previous_mode"]),
            )
        else:
            self.netplan_path.unlink(missing_ok=True)
        self._run_checked(("/usr/sbin/netplan", "generate"))
        self._run_checked(("/usr/sbin/netplan", "apply"))

    def apply(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = str(plan["operation_id"])
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        if transaction["plan"] != dict(plan) or transaction["state"] != "claimed":
            raise NetworkError("claimed network transaction differs from retained plan")
        runtime_stopped = False
        if plan["connectivity_impacting"]:
            if not self.maintenance_safe():
                raise NetworkError(
                    "network mutation requires landed, disarmed maintenance-safe state"
                )
            self.stop_runtime()
            runtime_stopped = True
        try:
            self._run_checked(
                (
                    "/usr/bin/systemctl",
                    "start",
                    f"iii-network-apply@{operation_id}.service",
                )
            )
        except Exception:
            if runtime_stopped:
                self.resume_runtime()
            raise
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        if transaction["state"] not in {"confirmed", "pending-confirmation"}:
            if runtime_stopped:
                self.resume_runtime()
            raise NetworkError(
                "privileged network apply did not reach a durable checkpoint"
            )
        if transaction["state"] == "confirmed" and runtime_stopped:
            self.resume_runtime()
        return {
            "kind": "network",
            "network_id": plan["network_id"],
            "state": transaction["state"],
            "confirmation_required": transaction["state"] == "pending-confirmation",
            **(
                {
                    "confirmation_deadline_s": CONFIRMATION_DEADLINE_S,
                    "deadline_monotonic_ns": transaction["deadline_monotonic_ns"],
                }
                if transaction["state"] == "pending-confirmation"
                else {}
            ),
        }

    def apply_claimed(self, operation_id: str) -> dict[str, Any]:
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        plan = transaction["plan"]
        if transaction["state"] != "claimed":
            raise NetworkError("network transaction is not awaiting privileged apply")
        if plan["no_change"]:
            transaction["state"] = "confirmed"
            self._save_transaction(transaction)
            (self._transaction_root(operation_id) / "desired.netplan").unlink(
                missing_ok=True
            )
            return {
                "kind": "network",
                "network_id": plan["network_id"],
                "state": "confirmed",
                "confirmation_required": False,
            }
        desired = self._transaction_root(operation_id) / "desired.netplan"
        if (
            desired.is_symlink()
            or not desired.is_file()
            or stat.S_IMODE(desired.stat().st_mode) & 0o077
        ):
            raise NetworkError(
                "claimed network profile is missing, linked, or not root-only"
            )
        previous = None
        previous_mode = None
        if self.netplan_path.exists() or self.netplan_path.is_symlink():
            if self.netplan_path.is_symlink() or not self.netplan_path.is_file():
                raise NetworkError("installed operator Netplan configuration is unsafe")
            previous = self.netplan_path.read_bytes()
            previous_mode = stat.S_IMODE(self.netplan_path.stat().st_mode)
            atomic_bytes(
                self._transaction_root(operation_id) / "previous.netplan",
                previous,
                mode=0o600,
            )
        transaction["previous_present"] = previous is not None
        transaction["previous_mode"] = previous_mode
        transaction["state"] = "applying"
        self._save_transaction(transaction)
        try:
            atomic_bytes(self.netplan_path, desired.read_bytes(), mode=0o600)
            self._run_checked(("/usr/sbin/netplan", "generate"))
            self._run_checked(("/usr/sbin/netplan", "apply"))
            now = self.monotonic_ns()
            transaction["state"] = "pending-confirmation"
            transaction["deadline_monotonic_ns"] = (
                now + CONFIRMATION_DEADLINE_S * 1_000_000_000
            )
            self._save_transaction(transaction)
            self._run_checked(
                (
                    "/usr/bin/systemctl",
                    "start",
                    f"iii-network-revert@{operation_id}.timer",
                )
            )
        except Exception as exc:
            transaction["failure"] = {
                "code": "network-apply-failed",
                "message": str(exc),
            }
            self._restore(transaction)
            transaction["state"] = "reverted"
            self._save_transaction(transaction)
            desired.unlink(missing_ok=True)
            raise
        return {
            "kind": "network",
            "network_id": plan["network_id"],
            "state": "pending-confirmation",
            "confirmation_required": True,
            "confirmation_deadline_s": CONFIRMATION_DEADLINE_S,
            "deadline_monotonic_ns": transaction["deadline_monotonic_ns"],
        }

    def confirm(
        self, operation_id: str, *, client_id: str, network_id: str
    ) -> dict[str, Any]:
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        if (
            transaction["client_id"] != client_id
            or transaction["network_id"] != network_id
        ):
            raise NetworkError("network confirmation binding mismatch")
        if transaction["state"] == "confirmed":
            return {
                "kind": "network-confirm",
                "network_id": network_id,
                "state": "confirmed",
            }
        if transaction["state"] != "pending-confirmation":
            raise NetworkError("network transaction is not awaiting confirmation")
        if (
            transaction["accepted_boot_id"] != self.boot_id()
            or self.monotonic_ns() > transaction["deadline_monotonic_ns"]
        ):
            self.request_revert(operation_id, reason="confirmation-deadline-expired")
            raise NetworkError(
                "network confirmation deadline expired; previous profile restored"
            )
        self._run_checked(
            ("/usr/bin/systemctl", "stop", f"iii-network-revert@{operation_id}.timer")
        )
        installed = self.netplan_path.read_bytes()
        metadata = {
            "schema": "iii.network-current/v1",
            "network_id": network_id,
            "netplan_sha256": hashlib.sha256(installed).hexdigest(),
            "confirmed_boot_id": self.boot_id(),
            "confirmed_monotonic_ns": self.monotonic_ns(),
        }
        atomic_document(self.state_root / "current.json", metadata, mode=0o600)
        transaction["state"] = "confirmed"
        self._save_transaction(transaction)
        (self._transaction_root(operation_id) / "desired.netplan").unlink(
            missing_ok=True
        )
        self.resume_runtime()
        return {
            "kind": "network-confirm",
            "network_id": network_id,
            "state": "confirmed",
        }

    def revert(
        self, operation_id: str, *, reason: str = "confirmation-timeout"
    ) -> dict[str, Any]:
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        if transaction["state"] == "reverted":
            return {
                "kind": "network-revert",
                "network_id": transaction["network_id"],
                "state": "reverted",
                "reason": reason,
            }
        if transaction["state"] not in {"applying", "pending-confirmation"}:
            raise NetworkError(
                "network transaction cannot be reverted from its current state"
            )
        self._restore(transaction)
        transaction["state"] = "reverted"
        transaction["failure"] = {"code": "network-not-confirmed", "message": reason}
        self._save_transaction(transaction)
        (self._transaction_root(operation_id) / "desired.netplan").unlink(
            missing_ok=True
        )
        return {
            "kind": "network-revert",
            "network_id": transaction["network_id"],
            "state": "reverted",
            "reason": reason,
        }

    def request_revert(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        self._run_checked(
            (
                "/usr/bin/systemctl",
                "start",
                f"iii-network-revert@{operation_id}.service",
            )
        )
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        if transaction["state"] != "reverted":
            raise NetworkError(
                "privileged network rollback did not restore the prior profile"
            )
        self.resume_runtime()
        return {
            "kind": "network-revert",
            "network_id": transaction["network_id"],
            "state": "reverted",
            "reason": reason,
        }

    def reconcile(self) -> dict[str, Any]:
        transactions = self.state_root / "transactions"
        recovered: list[str] = []
        if not transactions.exists():
            return {"schema": STATUS_SCHEMA, "recovered": recovered}
        for directory in sorted(transactions.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                raise NetworkError("network transaction root contains an unsafe entry")
            transaction = _load_object(
                directory / "transaction.json", label="network transaction"
            )
            if transaction["state"] in {"applying", "pending-confirmation"} and (
                transaction["accepted_boot_id"] != self.boot_id()
                or transaction["deadline_monotonic_ns"] is None
                or self.monotonic_ns() > transaction["deadline_monotonic_ns"]
            ):
                self.request_revert(
                    transaction["operation_id"], reason="receiver-reconciliation"
                )
                recovered.append(transaction["operation_id"])
        return {"schema": STATUS_SCHEMA, "recovered": recovered}

    def status(self, operation_id: str) -> dict[str, Any]:
        transaction = _load_object(
            self._transaction_path(operation_id), label="network transaction"
        )
        return {"schema": STATUS_SCHEMA, **transaction}

    def resume_after_transaction(self) -> None:
        self.resume_runtime()


def revert_main() -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(prog="iii-network-revert")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    controller = NetworkController(monotonic_ns=time.monotonic_ns)
    try:
        result = controller.revert(args.operation_id)
    except ContractError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def apply_main() -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(prog="iii-network-apply")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    controller = NetworkController(monotonic_ns=time.monotonic_ns)
    try:
        result = controller.apply_claimed(args.operation_id)
    except ContractError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
