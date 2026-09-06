"""Fixed-path onboard adapters for receiver-owned activation.

The application runtime publishes observations only.  Root-owned receiver code
independently verifies receiver/bootstrap readiness, systemd state, selector
identity, freshness, and the signed profile policy before accepting a release.
"""

from __future__ import annotations

import json
import errno
from pathlib import Path
import socket
import subprocess
import time
from typing import Any, Callable, Mapping

from iii_deployment.activation import ActivationSafetySnapshot, ActivationTuple
from iii_deployment.activation_health import (
    ActivationHealthPending,
    ActivationHealthPolicy,
    ActivationHealthSnapshot,
    ControlPlaneProof,
)
from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.update import READINESS_SCHEMA

RUNTIME_HEALTH_SCHEMA = "iii.runtime-activation-health/v1"
RUNTIME_HEALTH_PATH = Path("/run/iii/runtime-activation-health.json")
SAFETY_PATH = Path("/run/iii/activation-safety.json")
RECEIVER_READINESS_PATH = Path("/run/iii/receiver-readiness.json")
DAEMON_SOCKET_PATH = Path("/run/iii/system_manager.sock")
CONTROL_UNITS = (
    "iii-runtime-api.service",
    "iii-system-daemon.service",
)
STOP_TARGET = "iii.target"


def _canonical_document(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def _boot_id() -> str:
    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
    except OSError as exc:
        raise ContractError(f"cannot read host boot identity: {exc}") from exc
    if not value:
        raise ContractError("host boot identity is empty")
    return value


class OnboardSafetyProvider:
    def __init__(self, path: Path = SAFETY_PATH):
        self.path = path

    def __call__(self) -> ActivationSafetySnapshot:
        value = _canonical_document(self.path, label="runtime activation safety state")
        try:
            snapshot = ActivationSafetySnapshot(**value)
        except (TypeError, KeyError) as exc:
            raise ContractError("runtime activation safety state is malformed") from exc
        snapshot.validate()
        return snapshot

    def maintenance_safe_for_clock_recovery(self) -> bool:
        """Require fresh landed/disarmed and ownership-free runtime evidence."""
        try:
            snapshot = self()
        except ContractError:
            return False
        return bool(
            snapshot.runtime_fresh
            and snapshot.px4_available
            and snapshot.px4_fresh
            and snapshot.armed is False
            and snapshot.in_air is False
            and snapshot.mission_fresh
            and snapshot.mission_active is False
            and snapshot.mission_control_owner is False
            and snapshot.operation_fresh
            and snapshot.custom_operation_active is False
            and snapshot.custom_operation_control_owner is False
            and snapshot.direct_operation_active is False
            and snapshot.reference_owner_active is False
        )


class OnboardControlPlane:
    """Use only fixed systemd units and the local daemon socket."""

    def __init__(
        self,
        *,
        daemon_socket: Path = DAEMON_SOCKET_PATH,
        units: tuple[str, ...] = CONTROL_UNITS,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if tuple(sorted(set(units))) != units or units != CONTROL_UNITS:
            raise ContractError(
                "activation control units differ from fixed host policy"
            )
        self.daemon_socket = daemon_socket
        self.units = units
        self.runner = runner
        self.socket_factory = socket_factory
        self.monotonic = monotonic
        self.sleep = sleep

    def _systemctl(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            ["/usr/bin/systemctl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl failed").strip()
            raise ContractError(f"fixed systemd operation failed: {detail}")
        return result

    def unit_state(self, unit: str) -> str:
        if unit not in {*self.units, STOP_TARGET}:
            raise ContractError(
                "activation attempted to inspect an undeclared systemd unit"
            )
        result = self._systemctl("is-active", unit, check=False)
        state = (result.stdout or "inactive").strip()
        if state not in {
            "active",
            "activating",
            "deactivating",
            "inactive",
            "failed",
        }:
            raise ContractError("systemd returned an unknown activation state")
        return state

    def stop_all_units(self) -> tuple[str, ...]:
        self._systemctl("stop", STOP_TARGET)
        expected = (STOP_TARGET, *self.units)
        deadline = self.monotonic() + 30.0
        pending = expected
        while pending and self.monotonic() < deadline:
            pending = tuple(
                unit
                for unit in expected
                if self.unit_state(unit) not in {"inactive", "failed"}
            )
            if pending:
                self.sleep(0.1)
        if pending:
            raise ContractError("III units did not stop: " + ", ".join(pending))
        return tuple(sorted(expected))

    def start(self, selected: ActivationTuple) -> ControlPlaneProof:
        selected.validate()
        self.boot_profile(selected.profile)
        value: dict[str, Any] = {
            "schema": "iii.activation-control-plane-proof/v1",
            "release_id": selected.release_id,
            "profile": selected.profile,
            "started_units": list(self.units),
            "autonomy_started": False,
            "proof_id": "0" * 64,
        }
        value["proof_id"] = content_identity(
            {key: item for key, item in value.items() if key != "proof_id"}
        )
        proof = ControlPlaneProof(
            release_id=value["release_id"],
            profile=value["profile"],
            started_units=tuple(value["started_units"]),
            autonomy_started=False,
            proof_id=value["proof_id"],
        )
        proof.validate(expected=selected)
        return proof

    def boot_profile(self, profile: str) -> dict[str, Any]:
        """Boot a fixed installed profile after the receiver clock gate opens."""
        if profile not in {"real", "opti_track", "hil"}:
            raise ContractError("clock-gate boot profile is not an onboard profile")
        self._systemctl("start", *self.units)
        self._daemon_request(
            {"command": "boot", "profile": profile}, connect_timeout_s=30.0
        )
        started = self._daemon_request(
            {
                "command": "start",
                "activate": True,
                "select_nodes": [],
                "include_dependencies": False,
            }
        )
        if started.get("success") is not True:
            raise ContractError("system daemon did not start the canonical profile")
        states = {unit: self.unit_state(unit) for unit in self.units}
        failed = sorted(unit for unit, state in states.items() if state != "active")
        if failed:
            raise ContractError(
                "activation control-plane units are not active: " + ", ".join(failed)
            )
        return {
            "schema": "iii.clock-gate-runtime-start/v1",
            "profile": profile,
            "unit_states": states,
            "autonomy_started": False,
        }

    def stop_runtime_graph(self) -> None:
        """Stop daemon-owned runtime processes while leaving the API supervised."""
        if not self.daemon_socket.exists() or self.daemon_socket.is_symlink():
            return
        result = self._daemon_request(
            {
                "command": "stop",
                "cleanup": False,
                "select_nodes": [],
                "include_dependencies": False,
            }
        )
        if result.get("success") is not True:
            raise ContractError("system daemon did not stop the runtime graph")

    def _daemon_request(
        self, payload: Mapping[str, Any], *, connect_timeout_s: float = 0.0
    ) -> dict[str, Any]:
        request = {**payload, "daemon_timeout_sec": 90.0}
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        deadline = self.monotonic() + connect_timeout_s
        while True:
            if self.daemon_socket.is_symlink():
                raise ContractError("system daemon socket is linked")
            client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(95.0)
            try:
                client.connect(str(self.daemon_socket))
                break
            except OSError as exc:
                client.close()
                if (
                    exc.errno in {errno.ENOENT, errno.ECONNREFUSED}
                    and self.monotonic() < deadline
                ):
                    self.sleep(0.1)
                    continue
                raise ContractError(f"system daemon request failed: {exc}") from exc
        try:
            client.sendall(raw.encode("utf-8"))
            response = b""
            while not response.endswith(b"\n"):
                block = client.recv(64 * 1024)
                if not block:
                    break
                response += block
                if len(response) > 1024 * 1024:
                    raise ContractError("system daemon response exceeds fixed limit")
        except OSError as exc:
            raise ContractError(f"system daemon request failed: {exc}") from exc
        finally:
            client.close()
        try:
            value = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("system daemon response is malformed") from exc
        if (
            not isinstance(value, dict)
            or value.get("ok") is not True
            or not isinstance(value.get("result"), dict)
        ):
            raise ContractError(
                "system daemon rejected activation command: "
                + str(value.get("error", "unknown error"))
            )
        return value["result"]


class OnboardHealthProvider:
    """Compose independent receiver/systemd proof with runtime observations."""

    def __init__(
        self,
        *,
        receiver_id: str,
        receiver_generation: int,
        control_plane: OnboardControlPlane,
        hardware_roles_provider: Callable[[], Mapping[str, Mapping[str, Any]]],
        runtime_path: Path = RUNTIME_HEALTH_PATH,
        readiness_path: Path = RECEIVER_READINESS_PATH,
        monotonic: Callable[[], float] = time.monotonic,
        boot_id: Callable[[], str] = _boot_id,
        maximum_age_s: float = 2.5,
    ) -> None:
        if maximum_age_s <= 0 or maximum_age_s > 5:
            raise ContractError("runtime activation health freshness bound is invalid")
        self.receiver_id = receiver_id
        self.receiver_generation = receiver_generation
        self.control_plane = control_plane
        self.runtime_path = runtime_path
        self.readiness_path = readiness_path
        self.monotonic = monotonic
        self.boot_id = boot_id
        self.maximum_age_s = maximum_age_s
        self.hardware_roles_provider = hardware_roles_provider

    def __call__(
        self, candidate: ActivationTuple, policy: ActivationHealthPolicy
    ) -> ActivationHealthSnapshot:
        if self.runtime_path.is_symlink():
            raise ContractError("runtime activation health observation is linked")
        if not self.runtime_path.is_file():
            raise ActivationHealthPending(
                "runtime activation health observation is not available yet"
            )
        runtime = _canonical_document(
            self.runtime_path, label="runtime activation health observation"
        )
        required = {
            "schema",
            "snapshot_id",
            "release_id",
            "profile",
            "boot_id",
            "observed_monotonic",
            "daemon",
            "runtime_api",
            "configuration",
            "hardware_roles",
            "services",
            "managed_nodes",
            "px4",
            "operations",
        }
        if set(runtime) != required or runtime["schema"] != RUNTIME_HEALTH_SCHEMA:
            raise ContractError("runtime activation health fields are malformed")
        if runtime["snapshot_id"] != content_identity(
            {key: item for key, item in runtime.items() if key != "snapshot_id"}
        ):
            raise ContractError("runtime activation health identity mismatch")
        now = self.monotonic()
        current_boot = self.boot_id()
        if runtime["boot_id"] != current_boot:
            raise ActivationHealthPending(
                "runtime activation health still belongs to the previous boot"
            )
        observed = runtime["observed_monotonic"]
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or observed > now
            or now - observed > self.maximum_age_s
        ):
            raise ActivationHealthPending("runtime activation health is not fresh yet")
        readiness = _canonical_document(
            self.readiness_path, label="receiver readiness observation"
        )
        expected_readiness = {
            "schema",
            "receiver_id",
            "generation",
            "socket_open",
            "self_tests_passed",
            "journal_compatible",
            "bootstrap_protocol",
            "cli_protocol",
            "request_protocol",
        }
        if (
            set(readiness) != expected_readiness
            or readiness["schema"] != READINESS_SCHEMA
        ):
            raise ContractError("receiver readiness observation is malformed")
        receiver_ready = all(
            readiness[field] is True
            for field in ("socket_open", "self_tests_passed", "journal_compatible")
        )
        units = {
            unit: self.control_plane.unit_state(unit)
            for unit in policy.required_systemd_units
        }
        value: dict[str, Any] = {
            "schema": "iii.activation-health/v1",
            "evidence_id": "0" * 64,
            "release_id": runtime["release_id"],
            "profile": runtime["profile"],
            "boot_id": runtime["boot_id"],
            "observed_monotonic": observed,
            "receiver": {
                "ready": receiver_ready,
                "receiver_id": readiness["receiver_id"],
                "generation": readiness["generation"],
            },
            "bootstrap": {
                "ready": receiver_ready and readiness["bootstrap_protocol"] == "1",
                "protocol_version": readiness["bootstrap_protocol"],
            },
            "daemon": runtime["daemon"],
            "runtime_api": runtime["runtime_api"],
            "configuration": runtime["configuration"],
            "hardware_roles": dict(self.hardware_roles_provider()),
            "services": runtime["services"],
            "managed_nodes": runtime["managed_nodes"],
            "systemd_units": units,
            "px4": runtime["px4"],
            "operations": runtime["operations"],
        }
        value["evidence_id"] = content_identity(
            {key: item for key, item in value.items() if key != "evidence_id"}
        )
        snapshot = ActivationHealthSnapshot(**value)
        snapshot.validate()
        return snapshot
