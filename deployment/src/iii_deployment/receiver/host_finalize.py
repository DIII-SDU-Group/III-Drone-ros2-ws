"""Fail-closed first-boot authority revocation and cloud-init sanitization."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
from typing import Any, Callable, Mapping, Sequence

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.state import atomic_bytes, atomic_document

BOOTSTRAP_USER = "iii-bootstrap"
REPORT_PATH = Path("/var/lib/iii/deployment/host-provisioning-report.json")
SANITIZED_PATHS = (
    Path("/boot/firmware/user-data"),
    Path("/boot/firmware/meta-data"),
    Path("/boot/firmware/network-config"),
    Path("/var/lib/cloud/seed/nocloud"),
    Path("/var/lib/cloud/seed/nocloud-net"),
    Path("/var/lib/cloud/instances"),
    Path("/etc/iii-bootstrap"),
    Path("/var/log/cloud-init.log"),
    Path("/var/log/cloud-init-output.log"),
    Path("/var/log/iii/bootstrap-cloud-init.log"),
    Path("/etc/netplan/50-cloud-init.yaml"),
)


def _under(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        raise ContractError("host finalization path must be absolute")
    return root.joinpath(*path.parts[1:])


def _document(path: Path, *, label: str) -> dict[str, Any]:
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


def _remove_without_following(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _default_run(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=True, stdin=subprocess.DEVNULL)


def _default_user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def _selector_slot(root: Path, selector: str) -> str:
    path = _under(root, Path(f"/opt/iii/receiver/selectors/{selector}"))
    if not path.is_symlink():
        raise ContractError(f"receiver {selector} selector is absent or not a symlink")
    target = os.readlink(path)
    resolved = (path.parent / target).resolve(strict=True)
    slots = _under(root, Path("/opt/iii/receiver/slots")).resolve(strict=True)
    try:
        relative = resolved.relative_to(slots)
    except ValueError as exc:
        raise ContractError(
            f"receiver {selector} selector escapes the slots root"
        ) from exc
    if len(relative.parts) != 1 or relative.name not in {"a", "b"}:
        raise ContractError(f"receiver {selector} selector is not an A/B slot")
    return relative.name


def _sanitize_sudoers(root: Path, run: Callable[[Sequence[str]], None]) -> list[str]:
    sudoers_root = _under(root, Path("/etc/sudoers.d"))
    changed: list[str] = []
    backups: dict[Path, bytes] = {}
    if sudoers_root.exists():
        for path in sorted(sudoers_root.iterdir()):
            if path.is_symlink():
                raise ContractError(f"sudoers fragment is linked: {path.name}")
            if not path.is_file():
                continue
            raw = path.read_bytes()
            lines = raw.decode("utf-8").splitlines()
            retained = [line for line in lines if BOOTSTRAP_USER not in line]
            if retained == lines:
                continue
            backups[path] = raw
            if retained:
                atomic_bytes(path, ("\n".join(retained) + "\n").encode(), mode=0o440)
            else:
                path.unlink()
            changed.append(path.name)
    try:
        if root == Path("/"):
            run(("/usr/sbin/visudo", "-cf", "/etc/sudoers"))
    except Exception:
        for path, raw in backups.items():
            atomic_bytes(path, raw, mode=0o440)
        raise
    return changed


def finalize_host(
    *,
    baseline_id: str,
    root: Path = Path("/"),
    run: Callable[[Sequence[str]], None] = _default_run,
    user_exists: Callable[[str], bool] = _default_user_exists,
) -> Mapping[str, Any]:
    root = root.resolve()
    if len(baseline_id) != 64 or any(
        character not in "0123456789abcdef" for character in baseline_id
    ):
        raise ContractError("host baseline ID must be lowercase SHA-256")
    existing_report_path = _under(root, REPORT_PATH)
    if existing_report_path.exists() or existing_report_path.is_symlink():
        existing = _document(existing_report_path, label="host provisioning report")
        expected_report_id = content_identity(
            {key: value for key, value in existing.items() if key != "report_id"}
        )
        if (
            existing.get("schema") != "iii.host-provisioning-report/v1"
            or existing.get("state") != "provisioned"
            or existing.get("baseline_id") != baseline_id
            or existing.get("report_id") != expected_report_id
        ):
            raise ContractError("existing host provisioning report is invalid")
        if user_exists(BOOTSTRAP_USER):
            raise ContractError("bootstrap user reappeared after host finalization")
        permanent_network = _under(root, Path("/etc/netplan/90-iii-operator.yaml"))
        if permanent_network.is_symlink() or not permanent_network.is_file():
            raise ContractError(
                "permanent network state is missing after host finalization"
            )
        residual = [
            str(path)
            for path in SANITIZED_PATHS
            if _under(root, path).exists() or _under(root, path).is_symlink()
        ]
        if residual:
            raise ContractError(
                "secret-bearing bootstrap paths reappeared: " + ", ".join(residual)
            )
        return existing
    health = _document(
        _under(root, Path("/var/lib/iii/deployment/host-baseline-report.json")),
        label="converged-host health report",
    )
    if (
        health.get("schema") != "iii.host-baseline-report/v1"
        or health.get("state") != "converged"
    ):
        raise ContractError("host health report is not converged")
    if health.get("baseline_id") != baseline_id:
        raise ContractError("host health report belongs to another baseline")
    shared_target_profile_id = health.get("shared_target_profile_id")
    if (
        not isinstance(shared_target_profile_id, str)
        or len(shared_target_profile_id) != 64
        or any(
            character not in "0123456789abcdef"
            for character in shared_target_profile_id
        )
    ):
        raise ContractError("host health report lacks the shared target profile")

    readiness = _document(
        _under(root, Path("/run/iii/receiver-readiness.json")),
        label="receiver readiness",
    )
    if (
        readiness.get("schema") != "iii.receiver-readiness/v1"
        or readiness.get("receiver_id") != health.get("receiver", {}).get("receiver_id")
        or readiness.get("generation") != health.get("receiver", {}).get("generation")
        or readiness.get("socket_open") is not True
        or readiness.get("self_tests_passed") is not True
    ):
        raise ContractError(
            "receiver readiness does not authenticate the converged host"
        )

    access = _document(
        _under(root, Path("/var/lib/iii/deployment/access-state.json")),
        label="receiver access state",
    )
    if access.get("schema") != "iii.receiver-access-state/v2":
        raise ContractError("receiver access state has an unsupported schema")
    expected_access_id = content_identity(
        {key: value for key, value in access.items() if key != "access_id"}
    )
    clients = access.get("clients")
    if access.get("access_id") != expected_access_id or not isinstance(clients, dict):
        raise ContractError("receiver access-state identity is invalid")
    required_machine_fields = {
        "machine_id",
        "label",
        "public_key",
        "runtime_token_sha256",
        "field_signer_id",
        "field_signer_public_key",
        "state",
        "field_signer_state",
        "added_by",
        "proved_by",
    }
    active = sorted(clients)
    if not active or any(
        not isinstance(record, dict)
        or set(record) != required_machine_fields
        or record.get("state") != "active"
        or record.get("field_signer_state") != "active"
        or record.get("proved_by") != client_id
        for client_id, record in clients.items()
    ):
        raise ContractError("permanent operator access is absent, pending, or revoked")
    receiver_config = _document(
        _under(root, Path("/etc/iii/deployment-receiver.json")),
        label="receiver configuration",
    )
    runtime_uid = receiver_config.get("runtime_uid")
    runtime_gid = receiver_config.get("runtime_gid")
    if (
        receiver_config.get("schema") != "iii.receiver-config/v1"
        or not isinstance(runtime_uid, int)
        or isinstance(runtime_uid, bool)
        or runtime_uid <= 0
        or not isinstance(runtime_gid, int)
        or isinstance(runtime_gid, bool)
        or runtime_gid <= 0
    ):
        raise ContractError("receiver configuration lacks runtime ownership")
    authorized_keys = _under(root, Path("/home/iii/.ssh/authorized_keys"))
    if authorized_keys.is_symlink() or not authorized_keys.is_file():
        raise ContractError(
            "permanent forced-command authorized_keys is absent or linked"
        )
    authorized_keys_metadata = authorized_keys.stat(follow_symlinks=False)
    if (
        authorized_keys_metadata.st_uid != runtime_uid
        or authorized_keys_metadata.st_gid != runtime_gid
        or authorized_keys_metadata.st_mode & 0o077
    ):
        raise ContractError(
            "permanent forced-command authorized_keys ownership is not SSH-readable"
        )
    key_lines = sorted(
        line
        for line in authorized_keys.read_text(encoding="ascii").splitlines()
        if line
    )
    expected_key_lines = sorted(
        'restrict,command="/usr/bin/iii-deployment-ssh-gateway --client-id '
        + client_id
        + '" '
        + clients[client_id]["public_key"]
        for client_id in active
    )
    if key_lines != expected_key_lines:
        raise ContractError(
            "permanent operator keys are not restricted to the receiver gateway"
        )

    runtime_verifiers = {
        "schema": "iii.runtime-api-client-verifiers/v1",
        "verifier_id": "0" * 64,
        "access_id": access["access_id"],
        "generation": access["generation"],
        "clients": sorted(
            (
                {
                    "machine_id": record["machine_id"],
                    "label": record["label"],
                    "token_sha256": record["runtime_token_sha256"],
                }
                for record in clients.values()
            ),
            key=lambda item: item["machine_id"],
        ),
    }
    runtime_verifiers["verifier_id"] = content_identity(
        {key: value for key, value in runtime_verifiers.items() if key != "verifier_id"}
    )
    observed_runtime_verifiers = _document(
        _under(
            root,
            Path("/var/lib/iii/deployment/runtime-api-client-verifiers.json"),
        ),
        label="Runtime API machine verifier projection",
    )
    if observed_runtime_verifiers != runtime_verifiers:
        raise ContractError(
            "Runtime API machine verifier projection differs from access state"
        )

    field_signers = {
        "schema_version": "1",
        "store_type": "iii.trusted-signers",
        "signers": sorted(
            (
                {
                    "signer_id": record["field_signer_id"],
                    "algorithm": "Ed25519",
                    "authority": "workstation-field",
                    "public_key": record["field_signer_public_key"],
                    "state": "active",
                }
                for record in clients.values()
            ),
            key=lambda item: item["signer_id"],
        ),
    }
    observed_field_signers = _document(
        _under(root, Path("/var/lib/iii/deployment/workstation-field-signers.json")),
        label="field signer trust projection",
    )
    if observed_field_signers != field_signers:
        raise ContractError("field signer trust projection differs from access state")

    current_slot = _selector_slot(root, "current")
    fallback_slot = _selector_slot(root, "fallback")
    if current_slot != fallback_slot:
        raise ContractError(
            "initial receiver current and recovery fallback selectors differ"
        )

    target_network = _under(root, Path("/etc/netplan/90-iii-operator.yaml"))
    source_network = _under(root, Path("/etc/netplan/50-cloud-init.yaml"))
    if target_network.is_symlink() or source_network.is_symlink():
        raise ContractError("cloud-init or permanent network configuration is linked")
    if not target_network.exists():
        if not source_network.is_file():
            raise ContractError("no cloud-init network state exists to preserve")
        atomic_bytes(target_network, source_network.read_bytes(), mode=0o600)
    if not target_network.is_file():
        raise ContractError("permanent operator network configuration is not a file")
    if root == Path("/"):
        run(("/usr/sbin/netplan", "generate"))

    atomic_bytes(
        _under(root, Path("/etc/cloud/cloud-init.disabled")),
        b"III host provisioning completed; cloud-init reruns are disabled.\n",
        mode=0o600,
    )
    for relative in SANITIZED_PATHS:
        _remove_without_following(_under(root, relative))
    sudoers_changed = _sanitize_sudoers(root, run)

    if user_exists(BOOTSTRAP_USER):
        # The finalizer itself is normally a root child of the still-open
        # bootstrap SSH session. Force removes the account database entry while
        # allowing that one already-authenticated command to return its proof.
        try:
            run(("/usr/sbin/userdel", "--force", "--remove", BOOTSTRAP_USER))
        except subprocess.SubprocessError:
            # shadow-utils reports the still-running authenticated process even
            # when --force has successfully removed the account and home. The
            # observable NSS result, not that warning exit, is authoritative.
            if user_exists(BOOTSTRAP_USER):
                raise
    if user_exists(BOOTSTRAP_USER):
        raise ContractError("bootstrap user still exists after revocation")
    _remove_without_following(_under(root, Path("/home/iii-bootstrap")))

    residual = [
        str(path)
        for path in SANITIZED_PATHS
        if _under(root, path).exists() or _under(root, path).is_symlink()
    ]
    if residual:
        raise ContractError(
            "secret-bearing bootstrap paths remain: " + ", ".join(residual)
        )

    report: dict[str, Any] = {
        "schema": "iii.host-provisioning-report/v1",
        "report_id": "",
        "state": "provisioned",
        "baseline_id": baseline_id,
        "target_definition_id": health.get("target_definition_id"),
        "shared_target_profile_id": shared_target_profile_id,
        "logical_target": health.get("logical_target"),
        "profile": health.get("profile"),
        "receiver_id": readiness["receiver_id"],
        "receiver_generation": readiness["generation"],
        "receiver_slot": current_slot,
        "access_id": access["access_id"],
        "active_operator_clients": active,
        "active_operator_machines": sorted(
            record["machine_id"] for record in clients.values()
        ),
        "network_configuration": "/etc/netplan/90-iii-operator.yaml",
        "cloud_init_disabled": True,
        "secret_bearing_seed_and_instance_data_removed": True,
        "bootstrap_user_removed": True,
        "bootstrap_sudoers_fragments_changed": sudoers_changed,
        "commissioned": False,
    }
    report["report_id"] = content_identity(
        {key: value for key, value in report.items() if key != "report_id"}
    )
    output = _under(root, REPORT_PATH)
    atomic_document(output, report, mode=0o640)
    committed = _document(output, label="committed host provisioning report")
    if committed != report:
        raise ContractError(
            "committed host provisioning report differs from final state"
        )
    return committed


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-host-bootstrap-finalize")
    parser.add_argument("--baseline-id", required=True)
    arguments = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ContractError("host bootstrap finalization requires root")
        result = finalize_host(baseline_id=arguments.baseline_id)
    except (ContractError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
