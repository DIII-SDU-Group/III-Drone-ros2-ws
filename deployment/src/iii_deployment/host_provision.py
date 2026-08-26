"""Retained, content-bound orchestration for the Ansible host baseline."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from iii_deployment.contracts import ContractError, ContractRegistry, content_identity


INPUT_SCHEMA = "iii.host-provisioning-input/v1"
PLAN_SCHEMA = "iii.host-provisioning-plan/v1"
REPORT_SCHEMA = "iii.host-provisioning-run/v1"
FILE_INPUT_FIELDS = (
    "bundle_trust_source",
    "release_status_trust_source",
    "receiver_update_trust_source",
    "operator_public_keys_source",
    "runtime_api_secret_source",
)
DIRECTORY_INPUT_FIELDS = ("receiver_bundle_source", "receiver_wheelhouse_source")


class HostProvisionError(ContractError):
    code = "III_HOST_PROVISION_ERROR"


class HostProvisionChangedError(HostProvisionError):
    code = "III_HOST_PROVISION_INPUT_CHANGED"


class HostProvisionDriftError(HostProvisionError):
    code = "III_HOST_PROVISION_DRIFT"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authorized_uids() -> set[int]:
    values = {os.geteuid()} if hasattr(os, "geteuid") else set()
    if hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        try:
            values.add(int(os.environ["SUDO_UID"]))
        except ValueError as exc:
            raise HostProvisionError("SUDO_UID is not a valid local identity") from exc
    return values


def _require_ignored(path: Path) -> None:
    for parent in (path.parent, *path.parents):
        if not (parent / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(parent), "check-ignore", "--quiet", "--", str(path)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        if result.returncode == 1:
            raise HostProvisionError("provisioning input must be Git-ignored")
        raise HostProvisionError(
            "cannot authenticate provisioning input Git-ignore state"
        )
    return


def _secure_regular_file(path: Path, *, label: str, ignored: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise HostProvisionError(f"{label} must be a real regular file")
    metadata = resolved.stat()
    if hasattr(os, "geteuid") and metadata.st_uid not in _authorized_uids():
        raise HostProvisionError(f"{label} must be owned by the invoking user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HostProvisionError(f"{label} permissions must be owner-only")
    if ignored:
        _require_ignored(resolved)
    return resolved


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostProvisionError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise HostProvisionError(f"{label} must be a JSON object")
    return value


def _tree_manifest(path: Path, *, label: str) -> dict[str, Any]:
    root = path.expanduser().resolve()
    if path.is_symlink() or root.is_symlink() or not root.is_dir():
        raise HostProvisionError(f"{label} must be a real directory")
    files: list[dict[str, Any]] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(root).as_posix()
        if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".retry"}:
            continue
        if candidate.is_symlink():
            raise HostProvisionError(f"{label} contains a symbolic link: {relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise HostProvisionError(f"{label} contains a special file: {relative}")
        metadata = candidate.stat()
        files.append(
            {
                "path": relative,
                "sha256": _sha256(candidate),
                "size": metadata.st_size,
                "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
            }
        )
    if not files:
        raise HostProvisionError(f"{label} is empty")
    return {
        "path": str(root),
        "content_id": content_identity({"files": files}),
        "files": files,
    }


def load_input(path: Path, *, schema_root: Path) -> tuple[dict[str, Any], Path]:
    source = _secure_regular_file(path, label="host provisioning input", ignored=True)
    value = _load_object(source, label="host provisioning input")
    ContractRegistry(schema_root).validate("host-provisioning-input", value)
    if value.get("schema") != INPUT_SCHEMA:
        raise HostProvisionError("unsupported host provisioning input")
    try:
        network = ipaddress.ip_network(str(value["operator_cidr"]), strict=True)
    except ValueError as exc:
        raise HostProvisionError("operator_cidr must be a canonical network") from exc
    if network.version != 4 or not network.is_private or network.is_loopback:
        raise HostProvisionError(
            "operator_cidr must be a private non-loopback IPv4 network"
        )
    base = source.parent
    for field in (*FILE_INPUT_FIELDS, *DIRECTORY_INPUT_FIELDS):
        candidate = Path(str(value[field])).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        value[field] = str(candidate.resolve())
    for field in FILE_INPUT_FIELDS:
        _secure_regular_file(Path(value[field]), label=field)
    for field in DIRECTORY_INPUT_FIELDS:
        _tree_manifest(Path(value[field]), label=field)
    return value, source


def _ansible_manifest(project_root: Path) -> dict[str, Any]:
    return _tree_manifest(project_root, label="Ansible project")


def _git_identity(repo: Path, *, label: str) -> dict[str, str]:
    def query(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    sha = query("rev-parse", "HEAD")
    branch = query("branch", "--show-current")
    if not branch:
        raise HostProvisionError(f"{label} is detached")
    return {
        "repository": str(repo.resolve()),
        "branch": branch,
        "old_sha": sha,
        "new_sha": sha,
        "mutation": "none",
    }


def build_plan(
    *,
    operation_id: str,
    target: str,
    inventory: Path,
    input_path: Path,
    schema_root: Path,
    ansible_root: Path,
    workspace_root: Path,
    cli_root: Path,
    ansible_playbook: Path | None = None,
) -> dict[str, Any]:
    if not target or any(character.isspace() for character in target):
        raise HostProvisionError(
            "inventory target must be a non-empty host pattern without whitespace"
        )
    inventory_source = inventory.expanduser().resolve()
    if (
        inventory.is_symlink()
        or inventory_source.is_symlink()
        or not inventory_source.is_file()
    ):
        raise HostProvisionError("Ansible inventory must be a real regular file")
    value, secure_input = load_input(input_path, schema_root=schema_root)
    executable = ansible_playbook or Path(shutil.which("ansible-playbook") or "")
    if not executable or not executable.is_file():
        raise HostProvisionError("ansible-playbook is not installed on the controller")
    ansible = _ansible_manifest(ansible_root)
    artifacts = {
        field: _tree_manifest(Path(value[field]), label=field)
        for field in DIRECTORY_INPUT_FIELDS
    }
    source_files = {
        field: {"path": value[field], "sha256": _sha256(Path(value[field]))}
        for field in FILE_INPUT_FIELDS
    }
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "operation_id": operation_id,
        "target": target,
        "target_class": value["target_class"],
        "logical_target": value["logical_target"],
        "profile": value["profile"],
        "inventory": {
            "path": str(inventory_source),
            "sha256": _sha256(inventory_source),
        },
        "input": {"path": str(secure_input), "sha256": _sha256(secure_input)},
        "ansible": ansible,
        "ansible_playbook": str(executable.resolve()),
        "artifacts": artifacts,
        "controller_inputs": source_files,
        "repositories": [
            _git_identity(workspace_root, label="workspace"),
            _git_identity(cli_root, label="III CLI"),
        ],
        "required_checks": [
            "signed-input-and-artifact-content-recheck",
            "first-convergence",
            "second-run-check-mode-zero-change",
            "receiver-readiness-and-recovery",
            "bootstrap-sanitization-and-authority-revocation",
        ],
        "declared_permissions": [
            "bootstrap-ssh",
            "root-become",
            "host-package-and-policy-convergence",
            "receiver-initial-install",
            "cloud-init-secret-sanitization",
            "bootstrap-user-removal",
        ],
        "mutations": [
            "converge pinned Ubuntu/ROS host baseline",
            "install signed receiver and forced-command operator access",
            "install operator-LAN firewall and slew-only time policy",
            "preserve network state and remove first-boot authority/secrets",
        ],
    }
    plan["content_id"] = content_identity(
        {key: item for key, item in plan.items() if key != "content_id"}
    )
    return plan


def _verify_plan(plan: Mapping[str, Any], *, schema_root: Path) -> dict[str, Any]:
    ContractRegistry(schema_root).validate("host-provisioning-plan", plan)
    if plan.get("schema") != PLAN_SCHEMA:
        raise HostProvisionChangedError(
            "retained host provisioning plan schema changed"
        )
    expected = content_identity(
        {key: value for key, value in plan.items() if key != "content_id"}
    )
    if plan.get("content_id") != expected:
        raise HostProvisionChangedError(
            "retained host provisioning plan identity changed"
        )
    value, input_path = load_input(
        Path(str(plan["input"]["path"])), schema_root=schema_root
    )
    checks = {
        "inventory": _sha256(Path(str(plan["inventory"]["path"]))),
        "input": _sha256(input_path),
    }
    if (
        checks["inventory"] != plan["inventory"]["sha256"]
        or checks["input"] != plan["input"]["sha256"]
    ):
        raise HostProvisionChangedError(
            "inventory or provisioning input changed after planning"
        )
    if _ansible_manifest(Path(str(plan["ansible"]["path"]))) != plan["ansible"]:
        raise HostProvisionChangedError("Ansible project changed after planning")
    for field in DIRECTORY_INPUT_FIELDS:
        if _tree_manifest(Path(value[field]), label=field) != plan["artifacts"][field]:
            raise HostProvisionChangedError(f"{field} changed after planning")
    for field in FILE_INPUT_FIELDS:
        if _sha256(Path(value[field])) != plan["controller_inputs"][field]["sha256"]:
            raise HostProvisionChangedError(f"{field} changed after planning")
    for repository in plan["repositories"]:
        current = _git_identity(
            Path(repository["repository"]), label="planned repository"
        )
        if current != repository:
            raise HostProvisionChangedError(
                "repository branch or SHA changed after planning"
            )
    return value


def _run_ansible(
    *,
    plan: Mapping[str, Any],
    values: Mapping[str, Any],
    playbook: str,
    check: bool,
) -> dict[str, Any]:
    project = Path(str(plan["ansible"]["path"]))
    with tempfile.TemporaryDirectory(prefix="iii-host-provision-") as temporary:
        root = Path(temporary)
        extra_vars = root / "extra-vars.json"
        result_path = root / "ansible-result.json"
        payload = {
            "iii_target_class": values["target_class"],
            "iii_logical_target": values["logical_target"],
            "iii_profile": values["profile"],
            "iii_offline": bool(values.get("offline", False)),
            "iii_provisioning_inputs": {
                key: values[key]
                for key in (
                    "operator_cidr",
                    *DIRECTORY_INPUT_FIELDS,
                    *FILE_INPUT_FIELDS,
                )
            },
        }
        extra_vars.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(extra_vars, 0o600)
        argv = [
            str(plan["ansible_playbook"]),
            "--inventory",
            str(plan["inventory"]["path"]),
            "--limit",
            str(plan["target"]),
            "--extra-vars",
            "@" + str(extra_vars),
            "--diff",
        ]
        if check:
            argv.append("--check")
        argv.append(str(project / "playbooks" / playbook))
        environment = dict(os.environ)
        environment.update(
            {
                "ANSIBLE_CONFIG": str(project / "ansible.cfg"),
                "ANSIBLE_NOCOLOR": "1",
                "III_ANSIBLE_RESULT_PATH": str(result_path),
                "III_ANSIBLE_CHECK_MODE": "1" if check else "0",
            }
        )
        completed = subprocess.run(
            argv,
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-40:])
            raise HostProvisionError(
                f"Ansible {playbook} failed with {completed.returncode}:\n{tail}"
            )
        if result_path.is_symlink() or not result_path.is_file():
            raise HostProvisionError(
                "Ansible did not emit an authenticated machine recap"
            )
        result = _load_object(result_path, label="Ansible machine recap")
        ContractRegistry(project.parent / "schemas/v1").validate(
            "ansible-run-result", result
        )
        if (
            result.get("schema") != "iii.ansible-run-result/v1"
            or result.get("check_mode") is not check
        ):
            raise HostProvisionError("Ansible machine recap contract is invalid")
        totals = result.get("totals")
        if (
            not isinstance(totals, dict)
            or totals.get("failures")
            or totals.get("unreachable")
        ):
            raise HostProvisionError(
                "Ansible machine recap reports failure or unreachable hosts"
            )
        return result


def check_plan(plan: Mapping[str, Any], *, schema_root: Path) -> Mapping[str, Any]:
    values = _verify_plan(plan, schema_root=schema_root)
    return _run_ansible(
        plan=plan, values=values, playbook="aircraft-converge.yml", check=True
    )


def apply_plan(plan: Mapping[str, Any], *, schema_root: Path) -> Mapping[str, Any]:
    values = _verify_plan(plan, schema_root=schema_root)
    first = _run_ansible(
        plan=plan, values=values, playbook="aircraft-converge.yml", check=False
    )
    values = _verify_plan(plan, schema_root=schema_root)
    second = _run_ansible(
        plan=plan, values=values, playbook="aircraft-converge.yml", check=True
    )
    if second["totals"]["changed"] != 0:
        raise HostProvisionDriftError(
            f"second convergence predicts {second['totals']['changed']} unintended change(s)"
        )
    final = _run_ansible(
        plan=plan, values=values, playbook="aircraft-finalize.yml", check=False
    )
    report = {
        "schema": REPORT_SCHEMA,
        "operation_id": plan["operation_id"],
        "plan_content_id": plan["content_id"],
        "target": plan["target"],
        "state": "provisioned",
        "runs": {
            "first_convergence": first,
            "idempotence_check": second,
            "finalization": final,
        },
    }
    report["report_id"] = content_identity(report)
    ContractRegistry(schema_root).validate("host-provisioning-run", report)
    return report
