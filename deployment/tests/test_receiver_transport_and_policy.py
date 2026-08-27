from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading

import pytest

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.receiver.config import ReceiverConfig, load_live_state
from iii_deployment.receiver.protocol import Request
from iii_deployment.receiver.transport import (
    UnixReceiverServer,
    authenticate_forced_ssh_peer,
)

ROOT = Path(__file__).resolve().parents[2]
CLIENT_ID = "a" * 64


def raw_request(action: str = "status", payload: dict | None = None) -> bytes:
    return canonical_json(
        {
            "protocol_version": "1",
            "action": action,
            "operation_id": "operation-0001",
            "client_id": CLIENT_ID,
            "payload": payload or {},
            "nonce": None,
        }
    )


def test_unix_socket_transport_is_bounded_canonical_and_has_no_tcp_listener(
    tmp_path: Path,
) -> None:
    observed: list[Request] = []
    server = UnixReceiverServer(
        socket_path=tmp_path / "receiver.sock",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
        handler=lambda request: observed.append(request) or {"state": "ok"},
        peer_authenticator=lambda _pid, _uid, _client_id: None,
    )
    server.open()
    assert server.socket is not None and server.socket.family == socket.AF_UNIX
    thread = threading.Thread(target=server.serve_once)
    thread.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(tmp_path / "receiver.sock"))
    client.sendall(raw_request() + b"\n")
    client.shutdown(socket.SHUT_WR)
    response = b""
    while True:
        block = client.recv(65536)
        if not block:
            break
        response += block
    client.close()
    thread.join(timeout=5)
    server.close()
    value = json.loads(response)
    assert response == canonical_json(value) + b"\n"
    assert value["ok"] is True and observed[0].action.value == "status"
    policy = json.loads(
        (ROOT / "deployment/receiver-policy.json").read_text(encoding="utf-8")
    )
    assert policy["transport"]["tcp_listener"] is False
    assert policy["transport"]["kind"] == "unix-domain-socket"


def test_local_process_cannot_impersonate_forced_ssh_credential() -> None:
    with pytest.raises(ContractError, match="authenticated client|sshd session"):
        authenticate_forced_ssh_peer(os.getpid(), os.getuid(), CLIENT_ID)


def test_hostile_paths_units_environment_and_noncanonical_requests_are_rejected() -> (
    None
):
    with pytest.raises(ContractError, match="fields do not match"):
        Request.parse(
            raw_request(
                "status",
                {
                    "path": "/etc/shadow",
                    "unit": "ssh.service",
                    "environment": {"LD_PRELOAD": "x"},
                },
            )
        )
    with pytest.raises(ContractError, match="canonical JSON"):
        Request.parse(json.dumps(json.loads(raw_request())).encode("utf-8"))
    with pytest.raises(ContractError, match="unsupported receiver action"):
        Request.parse(
            canonical_json(
                {
                    "protocol_version": "1",
                    "action": "run-shell",
                    "operation_id": "operation-0001",
                    "client_id": CLIENT_ID,
                    "payload": {},
                    "nonce": "b" * 64,
                }
            )
        )


def test_receiver_config_and_live_state_are_fixed_canonical_contracts(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "receiver.json"
    config_path.write_bytes(
        canonical_json(
            {
                "schema": "iii.receiver-config/v1",
                "receiver_generation": 1,
                "logical_target": "drone",
                "profile": "real",
                "runtime_uid": 1000,
                "runtime_gid": 1000,
            }
        )
        + b"\n"
    )
    config = ReceiverConfig.load(config_path, production=False)
    assert config.logical_target == "drone" and config.receiver_generation == 1
    with pytest.raises(ContractError, match="fixed host policy"):
        ReceiverConfig.load(config_path, production=True)

    live_path = tmp_path / "live.json"
    live = {
        "schema": "iii.receiver-live-state/v1",
        "target_state_hash": "",
        "active_release_id": None,
        "configuration_hash": "b" * 64,
        "commissioning_hash": "c" * 64,
        "profile": "real",
    }
    live["target_state_hash"] = content_identity(
        {key: value for key, value in live.items() if key != "target_state_hash"}
    )
    live_path.write_bytes(canonical_json(live) + b"\n")
    assert (
        load_live_state(live_path, profile="real")["target_state_hash"]
        == live["target_state_hash"]
    )
    live["profile"] = "sim"
    live_path.write_bytes(canonical_json(live) + b"\n")
    with pytest.raises(ContractError, match="identity mismatch"):
        load_live_state(live_path, profile="real")


def test_host_policy_has_no_unrestricted_sudo_and_protects_receiver_bootstrap() -> None:
    privilege = json.loads(
        (ROOT / "deployment/host/receiver-privilege-policy.json").read_text(
            encoding="utf-8"
        )
    )
    sudoers = (ROOT / "deployment/host/iii-deployment-final.sudoers").read_text(
        encoding="utf-8"
    )
    receiver_policy = json.loads(
        (ROOT / "deployment/receiver-policy.json").read_text(encoding="utf-8")
    )
    ContractRegistry(ROOT / "deployment/schemas/v1").validate(
        "receiver-policy", receiver_policy
    )
    assert privilege["final_passwordless_sudo_commands"] == []
    assert "NOPASSWD" not in sudoers and "ALL=(ALL)" not in sudoers
    assert privilege["complete_key_loss"]["in_band_bypass"] is False
    forbidden = set(receiver_policy["normal_release_forbidden_paths"])
    assert "/opt/iii/receiver/bootstrap" in forbidden
    assert "/etc/iii/trust" in forbidden
    assert any("iii-deployment-receiver.service" in path for path in forbidden)
    assert set(receiver_policy["declared_systemd_units"]) == {
        "iii-system-daemon.service",
        "iii-runtime-api.service",
    }
    assert json.loads(
        (ROOT / "deployment/host/receiver-bootstrap-protocol.json").read_text()
    ) == {"schema": "iii.receiver-bootstrap-protocol/v1", "protocol": "1"}
    assert json.loads(
        (ROOT / "deployment/host/receiver-cli-protocol.json").read_text()
    ) == {"schema": "iii.receiver-cli-protocol/v1", "protocol": "1"}


def test_stable_units_run_receiver_outside_release_tree_and_allow_only_declared_writes() -> (
    None
):
    policy = json.loads(
        (ROOT / "deployment/receiver-policy.json").read_text(encoding="utf-8")
    )
    allowed = (
        set(policy["normal_release_mutable_paths"])
        | set(policy["self_update_receiver_mutable_paths"])
        | set(policy["host_control_mutable_paths"])
        | {"/var/lib/iii/incoming", "/run/iii"}
    )
    for name in (
        "iii-deployment-receiver-reconcile.service",
        "iii-deployment-receiver.service",
    ):
        unit = (ROOT / "deployment/systemd" / name).read_text(encoding="utf-8")
        exec_line = next(
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        )
        assert exec_line.startswith("ExecStart=/opt/iii/receiver/selectors/current/")
        assert "/opt/iii/releases/" not in exec_line
        assert "RestrictAddressFamilies=AF_UNIX" in unit
        write_line = next(
            line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
        )
        assert set(write_line.removeprefix("ReadWritePaths=").split()) <= allowed
        read_only_line = next(
            line for line in unit.splitlines() if line.startswith("ReadOnlyPaths=")
        )
        read_only = set(read_only_line.removeprefix("ReadOnlyPaths=").split())
        assert read_only == set(policy["normal_release_read_only_paths"])
        assert all(
            any(path == root or path.startswith(root + "/") for root in read_only)
            for path in policy["normal_release_forbidden_paths"]
            if path.startswith("/opt/iii/receiver/")
        )
        assert not any(
            path in write_line
            for path in policy["normal_release_forbidden_paths"]
            if path not in policy["host_control_mutable_paths"]
        )

    for name in (
        "iii-receiver-bootstrap-apply.service",
        "iii-receiver-bootstrap-reconcile.service",
    ):
        bootstrap = (ROOT / "deployment/systemd" / name).read_text(encoding="utf-8")
        assert "ExecStart=/opt/iii/receiver/bootstrap/" in bootstrap
        bootstrap_writes = next(
            line
            for line in bootstrap.splitlines()
            if line.startswith("ReadWritePaths=")
        )
        assert set(bootstrap_writes.removeprefix("ReadWritePaths=").split()) <= set(
            policy["self_update_bootstrap_mutable_paths"]
        )
        assert not any(
            path in bootstrap_writes for path in policy["self_update_forbidden_paths"]
        )
