from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from iii_deployment.activation import ActivationSafetySnapshot, ActivationTuple
from iii_deployment.activation_health import ActivationHealthPolicy
from iii_deployment.activation_runtime import (
    OnboardControlPlane,
    OnboardHealthProvider,
    OnboardSafetyProvider,
)
from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)


RELEASE = "a" * 64
CHECKPOINT = "b" * 64
RECEIVER = "c" * 64
REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _candidate(tmp_path: Path) -> ActivationTuple:
    return ActivationTuple(
        release_id=RELEASE,
        release_path=str(tmp_path / "opt/iii/releases" / RELEASE),
        configuration_checkpoint_id=CHECKPOINT,
        configuration_checkpoint_path=str(
            tmp_path / "var/lib/iii/configuration/checkpoints" / CHECKPOINT
        ),
        configuration_schema_version=1,
        mission_catalog_hash="sha256:" + "d" * 64,
        profile="real",
    )


def _policy() -> ActivationHealthPolicy:
    return ActivationHealthPolicy(
        required_hardware_roles=("fmu",),
        optional_hardware_roles=(),
        required_services=("micro_ros_agent",),
        optional_services=(),
        required_managed_nodes={"configuration_server": "active"},
        optional_managed_nodes={},
        required_systemd_units=(
            "iii-runtime-api.service",
            "iii-system-daemon.service",
        ),
    )


class Runner:
    def __init__(self) -> None:
        self.commands = []
        self.states = {
            "iii.target": "inactive",
            "iii-runtime-api.service": "active",
            "iii-system-daemon.service": "active",
        }

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[1] == "stop":
            for unit in self.states:
                self.states[unit] = "inactive"
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "start":
            for unit in command[2:]:
                self.states[unit] = "active"
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "is-active":
            state = self.states[command[2]]
            return subprocess.CompletedProcess(
                command, 0 if state == "active" else 3, state + "\n", ""
            )
        raise AssertionError(command)


class Socket:
    def __init__(self, *_args):
        self.response = b""
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def connect(self, _path):
        pass

    def sendall(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        result = (
            {"success": True}
            if request["command"] in {"start", "stop"}
            else {"booted": True}
        )
        self.response = json.dumps({"ok": True, "result": result}).encode() + b"\n"

    def recv(self, _maximum):
        response, self.response = self.response, b""
        return response

    def close(self):
        pass


def test_control_plane_uses_fixed_argv_stops_all_units_and_starts_no_autonomy(
    tmp_path: Path,
):
    runner = Runner()
    daemon_socket = tmp_path / "system-manager.sock"
    daemon_socket.touch()
    sockets = []

    def socket_factory(*args):
        value = Socket(*args)
        sockets.append(value)
        return value

    control = OnboardControlPlane(
        daemon_socket=daemon_socket,
        runner=runner,
        socket_factory=socket_factory,
        monotonic=lambda: 1.0,
        sleep=lambda _duration: None,
    )
    stopped = control.stop_all_units()
    assert set(stopped) == {
        "iii.target",
        "iii-runtime-api.service",
        "iii-system-daemon.service",
    }
    proof = control.start(_candidate(tmp_path))
    assert proof.autonomy_started is False
    assert [item[0] for item in runner.commands if item[0][1] in {"stop", "start"}] == [
        ["/usr/bin/systemctl", "stop", "iii.target"],
        [
            "/usr/bin/systemctl",
            "start",
            "iii-runtime-api.service",
            "iii-system-daemon.service",
        ],
    ]
    assert [socket.sent[0]["command"] for socket in sockets] == ["boot", "start"]
    assert sockets[1].sent[0]["activate"] is True
    assert "mission" not in json.dumps(sockets[1].sent[0]).lower()


def test_runtime_graph_stop_leaves_independent_api_unit_online(tmp_path: Path):
    runner = Runner()
    daemon_socket = tmp_path / "system-manager.sock"
    daemon_socket.touch()
    sockets = []

    def socket_factory(*args):
        value = Socket(*args)
        sockets.append(value)
        return value

    control = OnboardControlPlane(
        daemon_socket=daemon_socket,
        runner=runner,
        socket_factory=socket_factory,
    )
    control.stop_runtime_graph()
    assert runner.states["iii-runtime-api.service"] == "active"
    assert runner.commands == []
    assert sockets[0].sent[0] == {
        "command": "stop",
        "cleanup": False,
        "select_nodes": [],
        "include_dependencies": False,
        "daemon_timeout_sec": 90.0,
    }


def _runtime_document(now: float) -> dict:
    value = {
        "schema": "iii.runtime-activation-health/v1",
        "snapshot_id": "0" * 64,
        "release_id": RELEASE,
        "profile": "real",
        "boot_id": "boot-a",
        "observed_monotonic": now,
        "daemon": {
            "available": True,
            "fresh": True,
            "release_id": RELEASE,
            "profile": "real",
        },
        "runtime_api": {
            "available": True,
            "fresh": True,
            "release_id": RELEASE,
            "profile": "real",
            "api_version": ">=2.0.0,<3.0.0",
        },
        "configuration": {
            "reconciled": True,
            "durable": True,
            "schema_valid": True,
            "checkpoint_id": CHECKPOINT,
            "schema_version": 1,
        },
        "hardware_roles": {"fmu": {"state": "present", "unambiguous": True}},
        "services": {"micro_ros_agent": {"alive": True, "ready": True}},
        "managed_nodes": {"configuration_server": "active"},
        "px4": {
            "available": True,
            "fresh": True,
            "interface_compatible": True,
            "firmware_compatible": True,
            "parameter_manifest_matches": True,
            "armed": False,
            "in_air": False,
            "failsafe": False,
            "nav_state": "hold",
        },
        "operations": {
            "fresh": True,
            "mission_active": False,
            "mission_control_owner": False,
            "custom_operation_active": False,
            "custom_operation_control_owner": False,
            "direct_operation_active": False,
            "reference_owner_active": False,
        },
    }
    value["snapshot_id"] = content_identity(
        {key: item for key, item in value.items() if key != "snapshot_id"}
    )
    return value


def _readiness() -> dict:
    return {
        "schema": "iii.receiver-readiness/v1",
        "receiver_id": RECEIVER,
        "generation": 7,
        "socket_open": True,
        "self_tests_passed": True,
        "journal_compatible": True,
        "bootstrap_protocol": "1",
        "cli_protocol": "1",
        "request_protocol": "1",
    }


def test_health_provider_composes_independent_receiver_and_systemd_evidence(
    tmp_path: Path,
):
    runner = Runner()
    runtime = tmp_path / "runtime.json"
    readiness = tmp_path / "readiness.json"
    _write(runtime, _runtime_document(100.0))
    _write(readiness, _readiness())
    control = OnboardControlPlane(daemon_socket=tmp_path / "daemon.sock", runner=runner)
    provider = OnboardHealthProvider(
        receiver_id=RECEIVER,
        receiver_generation=7,
        control_plane=control,
        runtime_path=runtime,
        readiness_path=readiness,
        monotonic=lambda: 101.0,
        boot_id=lambda: "boot-a",
    )
    snapshot = provider(_candidate(tmp_path), _policy())
    REGISTRY.validate("runtime-activation-health", _runtime_document(100.0))
    REGISTRY.validate("activation-health", snapshot.as_document())
    assert snapshot.receiver == {
        "ready": True,
        "receiver_id": RECEIVER,
        "generation": 7,
    }
    assert snapshot.systemd_units == {
        "iii-runtime-api.service": "active",
        "iii-system-daemon.service": "active",
    }


def test_health_provider_rejects_stale_or_other_boot_runtime_observation(
    tmp_path: Path,
):
    runner = Runner()
    runtime = tmp_path / "runtime.json"
    readiness = tmp_path / "readiness.json"
    _write(runtime, _runtime_document(90.0))
    _write(readiness, _readiness())
    provider = OnboardHealthProvider(
        receiver_id=RECEIVER,
        receiver_generation=7,
        control_plane=OnboardControlPlane(
            daemon_socket=tmp_path / "daemon.sock", runner=runner
        ),
        runtime_path=runtime,
        readiness_path=readiness,
        monotonic=lambda: 101.0,
        boot_id=lambda: "boot-a",
    )
    with pytest.raises(ContractError, match="stale"):
        provider(_candidate(tmp_path), _policy())
    value = _runtime_document(101.0)
    value["boot_id"] = "boot-b"
    value["snapshot_id"] = content_identity(
        {key: item for key, item in value.items() if key != "snapshot_id"}
    )
    _write(runtime, value)
    with pytest.raises(ContractError, match="another boot"):
        provider(_candidate(tmp_path), _policy())


def test_safety_provider_rejects_tampering(tmp_path: Path):
    path = tmp_path / "safety.json"
    snapshot = ActivationSafetySnapshot(
        logical_target="drone",
        profile="real",
        observation_id="0" * 64,
        runtime_api_available=True,
        runtime_identity_matches=True,
        runtime_fresh=True,
        px4_available=True,
        px4_fresh=True,
        armed=False,
        in_air=False,
        nav_state="hold",
        failsafe=False,
        mission_fresh=True,
        mission_active=False,
        mission_control_owner=False,
        operation_fresh=True,
        custom_operation_active=False,
        custom_operation_control_owner=False,
        direct_operation_active=False,
        reference_owner_active=False,
        configuration_migration_ready=True,
        configuration_checkpoint_id=CHECKPOINT,
        continuously_safe_for_s=3.0,
    )
    value = asdict(snapshot)
    value["observation_id"] = content_identity(
        {key: item for key, item in value.items() if key != "observation_id"}
    )
    _write(path, value)
    assert OnboardSafetyProvider(path)().armed is False
    value["armed"] = True
    _write(path, value)
    with pytest.raises(ContractError, match="identity mismatch"):
        OnboardSafetyProvider(path)()
