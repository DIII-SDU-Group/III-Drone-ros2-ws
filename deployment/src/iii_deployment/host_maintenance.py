"""Receiver-owned, Ansible-executed host maintenance with reboot reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Callable, Mapping, Sequence

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.boot_baseline import (
    BootInspector,
    load_boot_profile,
    validate_boot_profile,
)
from iii_deployment.receiver.state import atomic_bytes, atomic_document
from iii_deployment.release_status import verify_status_index
from iii_deployment.signers import (
    load_trusted_signers,
    validate_trusted_signers,
    verify_signer_proof,
)

POLICY_SCHEMA = "iii.host-maintenance-policy/v1"
REQUEST_SCHEMA = "iii.host-maintenance-request/v1"
PLAN_SCHEMA = "iii.host-maintenance-plan/v1"
TRANSACTION_SCHEMA = "iii.host-maintenance-transaction/v1"
POLICY_PATH = Path("/etc/iii/host-maintenance-policy.json")
PLAYBOOK_PATH = Path("/usr/share/iii/host-maintenance/aircraft-maintenance.yml")
EXECUTOR_PATH = Path("/etc/systemd/system/iii-host-maintenance@.service")
STATE_ROOT = Path("/var/lib/iii/deployment/host-maintenance")
HOST_REPORT_PATH = Path("/var/lib/iii/deployment/host-baseline-report.json")
RECOMMISSION_PATH = Path("/var/lib/iii/deployment/recommission-required.json")
RELEASE_STATE_PATH = Path("/var/lib/iii/deployment/release-state.json")
STATUS_INDEX_PATH = Path("/var/lib/iii/deployment/release-status-index.json")
RELEASE_ROOT = Path("/opt/iii/releases")
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
HASH = re.compile(r"^[a-f0-9]{64}$")
FIXED_PLATFORM = {
    "distribution": "Ubuntu",
    "release": "24.04",
    "codename": "noble",
    "architecture": "aarch64",
    "ros_distro": "jazzy",
}
TRUST_KINDS = {
    "bundle-trust": ("bundle", "ci-qualified"),
    "release-status-trust": ("release_status", "release-status"),
}


class HostMaintenanceError(ContractError):
    code = "III_HOST_MAINTENANCE_REJECTED"


class HostMaintenanceChanged(HostMaintenanceError):
    code = "III_HOST_MAINTENANCE_PLAN_CHANGED"


class HostMaintenanceRecoveryRequired(HostMaintenanceError):
    code = "III_HOST_MAINTENANCE_RECOVERY_REQUIRED"


def _under(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        raise HostMaintenanceError("host-maintenance paths must be absolute")
    return root.joinpath(*path.parts[1:])


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise HostMaintenanceError(f"maintenance input is missing or linked: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise HostMaintenanceError(f"cannot hash maintenance input: {exc}") from exc
    return digest.hexdigest()


def _object(path: Path, *, label: str, canonical: bool = False) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HostMaintenanceError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostMaintenanceError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise HostMaintenanceError(f"{label} must contain a JSON object")
    if canonical and raw != canonical_json(value) + b"\n":
        raise HostMaintenanceError(f"{label} is not canonical JSON")
    return value


def _identity(value: Mapping[str, Any], field: str) -> str:
    return content_identity({key: item for key, item in value.items() if key != field})


def validate_policy(
    value: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    if value.get("platform") != FIXED_PLATFORM:
        raise HostMaintenanceError(
            "major Ubuntu or ROS transitions require SD-card reprovisioning"
        )
    registry.validate("host-maintenance-policy", value)
    if value.get("schema") != POLICY_SCHEMA or value.get("policy_id") != _identity(
        value, "policy_id"
    ):
        raise HostMaintenanceError("host-maintenance policy identity is invalid")
    packages = value.get("governed_packages")
    if not isinstance(packages, list) or packages != sorted(set(packages)):
        raise HostMaintenanceError(
            "governed maintenance packages must be unique and sorted"
        )
    return dict(value)


def validate_request(
    value: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    registry.validate("host-maintenance-request", value)
    if value.get("schema") != REQUEST_SCHEMA or value.get("request_id") != _identity(
        value, "request_id"
    ):
        raise HostMaintenanceError("host-maintenance request identity is invalid")
    policy = validate_policy(value.get("policy", {}), registry)
    kind = value.get("kind")
    trust_store = value.get("trust_store")
    status_index = value.get("release_status_index")
    retired = value.get("retire_signer_ids")
    proofs = value.get("replacement_proofs")
    boot_profile = value.get("boot_profile")
    if kind in {"packages", "boot-settings"}:
        if trust_store is not None or status_index is not None or retired or proofs:
            raise HostMaintenanceError(
                "package/boot and trust-root changes require separate plans"
            )
        if kind == "packages" and boot_profile is not None:
            raise HostMaintenanceError(
                "package and boot-setting changes require separate plans"
            )
        if kind == "boot-settings":
            if value.get("offline") is not False or not isinstance(boot_profile, dict):
                raise HostMaintenanceError(
                    "boot-setting maintenance requires one explicit boot profile"
                )
            try:
                boot_profile = validate_boot_profile(boot_profile, registry)
            except ContractError as exc:
                raise HostMaintenanceError(f"boot profile is invalid: {exc}") from exc
    else:
        if boot_profile is not None:
            raise HostMaintenanceError(
                "trust-root rotation cannot carry a boot profile"
            )
        if value.get("offline") is not False or not isinstance(trust_store, dict):
            raise HostMaintenanceError(
                "trust-root rotation cannot carry package/offline mutations"
            )
        if not retired:
            raise HostMaintenanceError(
                "trust-root rotation must name every signer being retired"
            )
        if kind == "release-status-trust" and not isinstance(status_index, dict):
            raise HostMaintenanceError(
                "release-status trust rotation requires a replacement signed index"
            )
        if kind == "bundle-trust" and status_index is not None:
            raise HostMaintenanceError(
                "bundle trust rotation cannot carry a release-status index"
            )
    return {**dict(value), "policy": policy, "boot_profile": boot_profile}


def _backup_reference(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = _object(path.expanduser().absolute(), label="host backup receipt")
    backup_id = value.get("backup_id") or value.get("receipt_id")
    if (
        value.get("schema") != "iii.host-backup-receipt/v1"
        or value.get("verified") is not True
        or not isinstance(backup_id, str)
        or not HASH.fullmatch(backup_id)
        or not isinstance(value.get("target_state_hash"), str)
        or not HASH.fullmatch(value["target_state_hash"])
    ):
        raise HostMaintenanceError(
            "maintenance backup evidence is not a verified state-bound receipt"
        )
    return {
        "schema": "iii.host-backup-receipt/v1",
        "backup_id": backup_id,
        "target_state_hash": value["target_state_hash"],
        "verified": True,
        "record_sha256": _sha256(path.expanduser().absolute()),
    }


def build_request(
    *,
    kind: str,
    policy_path: Path,
    registry: ContractRegistry,
    offline: bool = False,
    backup_record: Path | None = None,
    boot_profile_path: Path | None = None,
    trust_store_path: Path | None = None,
    release_status_index_path: Path | None = None,
    retire_signer_ids: Sequence[str] = (),
    replacement_proof_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    policy = validate_policy(
        _object(policy_path.expanduser().absolute(), label="host-maintenance policy"),
        registry,
    )
    trust_store = None
    if trust_store_path is not None:
        trust_store = load_trusted_signers(
            trust_store_path.expanduser().absolute(), registry
        )
    release_status_index = None
    if release_status_index_path is not None:
        release_status_index = _object(
            release_status_index_path.expanduser().absolute(),
            label="replacement release-status index",
            canonical=True,
        )
    replacement_proofs = [
        _object(
            path.expanduser().absolute(),
            label="replacement signer proof",
            canonical=True,
        )
        for path in replacement_proof_paths
    ]
    boot_profile = None
    if boot_profile_path is not None:
        boot_profile = load_boot_profile(
            boot_profile_path.expanduser().absolute(), registry
        )
    value: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "request_id": "0" * 64,
        "kind": kind,
        "policy": policy,
        "offline": bool(offline),
        "backup": _backup_reference(backup_record),
        "boot_profile": boot_profile,
        "trust_store": trust_store,
        "release_status_index": release_status_index,
        "retire_signer_ids": sorted(set(retire_signer_ids)),
        "replacement_proofs": sorted(
            replacement_proofs, key=lambda item: str(item.get("signer_id"))
        ),
    }
    value["request_id"] = _identity(value, "request_id")
    return validate_request(value, registry)


class HostMaintenanceController:
    """Plan and apply one narrow host mutation under receiver ownership."""

    def __init__(
        self,
        *,
        root: Path,
        registry: ContractRegistry,
        policy_path: Path = POLICY_PATH,
        playbook_path: Path = PLAYBOOK_PATH,
        executor_path: Path = EXECUTOR_PATH,
        maintenance_safe: Callable[[], bool] = lambda: False,
        stop_runtime: Callable[[], Sequence[str]] = lambda: (),
        resume_runtime: Callable[[], Any] = lambda: None,
        reboot: Callable[[], Any] | None = None,
        snapshot_provider: Callable[[], Mapping[str, Any]] | None = None,
        package_planner: (
            Callable[[Mapping[str, Any], bool], Sequence[str]] | None
        ) = None,
        ansible_runner: Callable[[Sequence[str], Mapping[str, str]], Any] | None = None,
        recovery_validator: Callable[[], Mapping[str, Any]] | None = None,
        active_operator_count: Callable[[], int] = lambda: 1,
    ) -> None:
        self.root = root.resolve()
        self.registry = registry
        self.policy_path = _under(self.root, policy_path)
        self.playbook_path = _under(self.root, playbook_path)
        self.executor_path = _under(self.root, executor_path)
        self.state_root = _under(self.root, STATE_ROOT)
        self.maintenance_safe = maintenance_safe
        self.stop_runtime = stop_runtime
        self.resume_runtime = resume_runtime
        self.reboot_host = reboot or self._default_reboot
        self.snapshot_provider = snapshot_provider or self._platform_snapshot
        self.package_planner = package_planner or self._plan_packages
        self.ansible_runner = ansible_runner or self._default_ansible_runner
        self.recovery_validator = recovery_validator or self._validate_protected_release
        self.active_operator_count = active_operator_count

    @property
    def current_path(self) -> Path:
        return self.state_root / "current.json"

    def _policy(self) -> dict[str, Any]:
        value = validate_policy(
            _object(self.policy_path, label="installed host-maintenance policy"),
            self.registry,
        )
        observed = self.policy_path.stat(follow_symlinks=False)
        if self.root == Path("/") and (
            observed.st_uid != 0 or observed.st_mode & 0o022
        ):
            raise HostMaintenanceError(
                "installed host-maintenance policy is not root-owned and write-protected"
            )
        return value

    def _playbook_sha256(self) -> str:
        observed = self.playbook_path.stat(follow_symlinks=False)
        if self.root == Path("/") and (
            observed.st_uid != 0 or observed.st_mode & 0o022
        ):
            raise HostMaintenanceError(
                "installed maintenance playbook is not root-owned and write-protected"
            )
        return _sha256(self.playbook_path)

    def _executor_sha256(self) -> str:
        observed = self.executor_path.stat(follow_symlinks=False)
        if self.root == Path("/") and (
            observed.st_uid != 0 or observed.st_mode & 0o022
        ):
            raise HostMaintenanceError(
                "installed maintenance executor is not root-owned and write-protected"
            )
        return _sha256(self.executor_path)

    def _platform_snapshot(self) -> Mapping[str, Any]:
        os_release = {}
        try:
            for line in (
                _under(self.root, Path("/etc/os-release"))
                .read_text(encoding="utf-8")
                .splitlines()
            ):
                if "=" in line:
                    key, item = line.split("=", 1)
                    os_release[key] = item.strip('"')
        except (OSError, UnicodeError) as exc:
            raise HostMaintenanceError(f"cannot inspect host platform: {exc}") from exc
        completed = subprocess.run(
            ["/usr/bin/dpkg-query", "-W", "-f=${binary:Package}\\t${Version}\\n"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if completed.returncode != 0:
            raise HostMaintenanceError("cannot inventory installed dpkg packages")
        packages: dict[str, list[str]] = {}
        for line in completed.stdout.splitlines():
            if "\t" not in line:
                continue
            name, version = line.split("\t", 1)
            name = name.split(":", 1)[0]
            packages.setdefault(name, []).append(version)
        boot_id = (
            _under(self.root, Path("/proc/sys/kernel/random/boot_id"))
            .read_text(encoding="ascii")
            .strip()
        )
        report = _object(
            _under(self.root, HOST_REPORT_PATH),
            label="host baseline report",
            canonical=True,
        )
        trust = {}
        for kind, (policy_key, _authority) in TRUST_KINDS.items():
            path = _under(self.root, Path(self._policy()["trust"][policy_key]["path"]))
            store = load_trusted_signers(path, self.registry)
            trust[kind] = content_identity(store)
        status_index = _object(
            _under(self.root, STATUS_INDEX_PATH),
            label="onboard release-status index",
            canonical=True,
        )
        boot_profile_path = _under(
            self.root, Path(self._policy()["boot"]["installed_profile_path"])
        )
        boot_profile = load_boot_profile(boot_profile_path, self.registry)
        boot = BootInspector(
            profile_path=boot_profile_path,
            registry=self.registry,
            root=self.root,
        ).inspect()
        value: dict[str, Any] = {
            "schema": "iii.host-maintenance-snapshot/v1",
            "snapshot_id": "0" * 64,
            "platform": {
                "distribution": os_release.get("NAME"),
                "release": os_release.get("VERSION_ID"),
                "codename": os_release.get("VERSION_CODENAME"),
                "architecture": subprocess.run(
                    ["/usr/bin/dpkg", "--print-architecture"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    timeout=10,
                ).stdout.strip(),
                "ros_distro": "jazzy",
                "kernel": os.uname().release,
                "boot_id": boot_id,
            },
            "host_contract": {
                "baseline_id": report.get("baseline_id"),
                "unit_contract_id": report.get("unit_contract_id"),
                "target_definition_id": report.get("target_definition_id"),
                "shared_target_profile_id": report.get("shared_target_profile_id"),
            },
            "packages": {
                key: sorted(set(item)) for key, item in sorted(packages.items())
            },
            "trust_store_ids": trust,
            "release_status_index_id": status_index.get("index_id"),
            "boot_profile_id": boot_profile["profile_id"],
            "boot": boot,
            "reboot_required": _under(
                self.root, Path("/var/run/reboot-required")
            ).exists(),
        }
        value["snapshot_id"] = _identity(value, "snapshot_id")
        return value

    @staticmethod
    def _apt_sources(policy: Mapping[str, Any]) -> tuple[str, str]:
        ubuntu = (
            "Types: deb\n"
            f"URIs: {policy['snapshots']['ubuntu']}\n"
            "Suites: noble noble-updates noble-security\n"
            "Components: main universe\n"
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
            "Check-Valid-Until: no\n"
        )
        ros = (
            "Types: deb\n"
            f"URIs: {policy['snapshots']['ros']}\n"
            "Suites: noble\n"
            "Components: main\n"
            "Signed-By: /usr/share/keyrings/ros-snapshot-builder.gpg\n"
            "Check-Valid-Until: no\n"
        )
        return ubuntu, ros

    def _plan_packages(self, policy: Mapping[str, Any], offline: bool) -> Sequence[str]:
        packages = policy["governed_packages"]
        temporary = tempfile.TemporaryDirectory(prefix="iii-apt-plan-")
        apt_root = Path(temporary.name)
        source_parts = apt_root / "sources.list.d"
        source_parts.mkdir(parents=True, mode=0o700)
        ubuntu, ros = self._apt_sources(policy)
        (source_parts / "ubuntu.sources").write_text(ubuntu, encoding="utf-8")
        (source_parts / "ros2.sources").write_text(ros, encoding="utf-8")
        apt_options = [
            "-o",
            "Dir::Etc::sourcelist=/dev/null",
            "-o",
            f"Dir::Etc::sourceparts={source_parts}",
            "-o",
            "Debug::NoLocking=1",
        ]
        if offline:
            if policy["snapshots"] != self._policy()["snapshots"]:
                temporary.cleanup()
                raise HostMaintenanceError(
                    "offline maintenance cannot change the installed signed snapshot selection"
                )
        else:
            lists = apt_root / "lists"
            archives = apt_root / "archives"
            for path in (lists / "partial", archives / "partial"):
                path.mkdir(parents=True, mode=0o700)
            apt_options.extend(
                [
                    "-o",
                    f"Dir::State::lists={lists}",
                    "-o",
                    f"Dir::Cache::archives={archives}",
                ]
            )
            refreshed = subprocess.run(
                ["/usr/bin/apt-get", *apt_options, "update"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=600,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
            if refreshed.returncode != 0:
                temporary.cleanup()
                raise HostMaintenanceError(
                    "desired signed package snapshots are unavailable before mutation"
                )
        completed = subprocess.run(
            [
                "/usr/bin/apt-get",
                *apt_options,
                "--simulate",
                "--no-remove",
                "install",
                *packages,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        if completed.returncode != 0 or any(
            line.startswith("Remv ") for line in completed.stdout.splitlines()
        ):
            temporary.cleanup()
            raise HostMaintenanceError(
                "governed package simulation failed or proposed removal"
            )
        changes = sorted(
            {
                line.split()[1].split(":", 1)[0]
                for line in completed.stdout.splitlines()
                if line.startswith(("Inst ", "Conf ")) and len(line.split()) > 1
            }
        )
        if offline and changes:
            cached = subprocess.run(
                [
                    "/usr/bin/apt-get",
                    *apt_options,
                    "--download-only",
                    "--no-download",
                    "--assume-yes",
                    "--no-remove",
                    "install",
                    *packages,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
            if cached.returncode != 0:
                raise HostMaintenanceError(
                    "offline maintenance cache is incomplete before mutation"
                )
        elif changes:
            available = subprocess.run(
                [
                    "/usr/bin/apt-get",
                    *apt_options,
                    "--download-only",
                    "--assume-yes",
                    "--no-remove",
                    "install",
                    *packages,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
            if available.returncode != 0:
                temporary.cleanup()
                raise HostMaintenanceError(
                    "a governed package is unavailable from the retained signed snapshots"
                )
        temporary.cleanup()
        return changes

    @staticmethod
    def _platform_matches(
        snapshot: Mapping[str, Any], policy: Mapping[str, Any]
    ) -> None:
        actual = dict(snapshot.get("platform", {}))
        actual.pop("kernel", None)
        actual.pop("boot_id", None)
        if actual.get("architecture") == "arm64":
            actual["architecture"] = "aarch64"
        if actual != policy["platform"]:
            raise HostMaintenanceError(
                "major Ubuntu/ROS/platform changes require SD-card reprovisioning"
            )
        if snapshot.get("host_contract") != policy["host_contract"]:
            raise HostMaintenanceError(
                "in-place maintenance cannot cross the governed host contract"
            )

    def _trust_change(
        self, request: Mapping[str, Any], before: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        kind = request["kind"]
        if kind not in TRUST_KINDS:
            return None, False
        policy_key, authority = TRUST_KINDS[kind]
        path = _under(self.root, Path(request["policy"]["trust"][policy_key]["path"]))
        current = load_trusted_signers(path, self.registry)
        try:
            proposed = validate_trusted_signers(request["trust_store"], self.registry)
        except ContractError as exc:
            raise HostMaintenanceError(
                f"replacement trust store is invalid: {exc}"
            ) from exc
        proposed_ids = [item["signer_id"] for item in proposed["signers"]]
        if proposed_ids != sorted(set(proposed_ids)):
            raise HostMaintenanceError("replacement trust store is not uniquely sorted")
        if any(item["authority"] != authority for item in proposed["signers"]):
            raise HostMaintenanceError("replacement trust store mixes authorities")
        active = [item for item in proposed["signers"] if item["state"] == "active"]
        if not active:
            raise HostMaintenanceError("trust rotation would strand the final signer")
        current_by_id = {item["signer_id"]: item for item in current["signers"]}
        proposed_by_id = {item["signer_id"]: item for item in proposed["signers"]}
        retired_ids = set(request["retire_signer_ids"])
        if not set(current_by_id).issubset(proposed_by_id):
            raise HostMaintenanceError(
                "trust rotation cannot erase historical signer records"
            )
        current_index = None
        proposed_index = request.get("release_status_index")
        if kind == "release-status-trust":
            current_index = _object(
                _under(self.root, STATUS_INDEX_PATH),
                label="onboard release-status index",
                canonical=True,
            )
            if before.get("release_status_index_id") != current_index.get("index_id"):
                raise HostMaintenanceChanged(
                    "release-status index changed during maintenance planning"
                )
            if (
                not isinstance(proposed_index, dict)
                or proposed_index.get("statements") != current_index.get("statements")
                or proposed_index.get("sequence") != current_index.get("sequence")
            ):
                raise HostMaintenanceError(
                    "replacement status index must preserve the exact historical statement chain"
                )
        transitioned: set[str] = set()
        for signer_id, prior in current_by_id.items():
            replacement = proposed_by_id[signer_id]
            stable = ("signer_id", "algorithm", "authority", "public_key")
            if any(prior[field] != replacement[field] for field in stable):
                raise HostMaintenanceError(
                    "trust rotation cannot rewrite historical signer metadata"
                )
            if prior["state"] == "revoked":
                if prior != replacement:
                    raise HostMaintenanceError(
                        "trust rotation cannot rewrite an earlier signer revocation"
                    )
                continue
            if replacement["state"] == "revoked":
                transitioned.add(signer_id)
                if kind == "release-status-trust":
                    boundary = {
                        "sequence": current_index["sequence"],
                        "statement_id": current_index["statements"][-1]["statement_id"],
                    }
                    if replacement.get("trusted_through") != boundary:
                        raise HostMaintenanceError(
                            "retired status signer must pin the exact commissioned history boundary"
                        )
                elif replacement.get("trusted_through") is not None:
                    raise HostMaintenanceError(
                        "bundle signer revocation cannot declare status history"
                    )
            elif replacement != prior:
                raise HostMaintenanceError(
                    "unchanged active signer metadata must remain exact"
                )
        if transitioned != retired_ids:
            raise HostMaintenanceError(
                "retired signer list must exactly match active-to-revoked transitions"
            )
        for signer_id, replacement in proposed_by_id.items():
            if signer_id not in current_by_id and replacement["state"] != "active":
                raise HostMaintenanceError(
                    "new trust entries must be usable active replacements"
                )
        new_active = {
            signer_id: replacement
            for signer_id, replacement in proposed_by_id.items()
            if signer_id not in current_by_id and replacement["state"] == "active"
        }
        proofs = {item["signer_id"]: item for item in request["replacement_proofs"]}
        if set(proofs) != set(new_active) or len(proofs) != len(
            request["replacement_proofs"]
        ):
            raise HostMaintenanceError(
                "replacement proofs must exactly cover every new active signer"
            )
        for signer_id, replacement in new_active.items():
            try:
                verify_signer_proof(replacement, proofs[signer_id])
            except ContractError as exc:
                raise HostMaintenanceError(
                    f"replacement signer {signer_id} lacks valid proof of possession"
                ) from exc
        for signer_id in request["retire_signer_ids"]:
            if (
                signer_id not in current_by_id
                or current_by_id[signer_id]["state"] != "active"
            ):
                raise HostMaintenanceError("retired signer is not currently active")
            if (
                signer_id not in proposed_by_id
                or proposed_by_id[signer_id]["state"] != "revoked"
            ):
                raise HostMaintenanceError(
                    "retired signer must remain visible as revoked history"
                )
        status_index_change = None
        if kind == "release-status-trust":
            latest = verify_status_index(proposed_index, proposed, self.registry)
            if proposed_index["signer_id"] in retired_ids:
                raise HostMaintenanceError(
                    "replacement status index must be signed by an active replacement"
                )
            if latest != verify_status_index(current_index, current, self.registry):
                raise HostMaintenanceError(
                    "trust rotation cannot alter resolved historical release statuses"
                )
            status_index_change = {
                "before_index_id": current_index["index_id"],
                "after_index_id": proposed_index["index_id"],
            }
        current_id = content_identity(current)
        if before["trust_store_ids"].get(kind) != current_id:
            raise HostMaintenanceChanged("trust store changed during planning")
        proposed_id = content_identity(proposed)
        return (
            {
                "kind": kind,
                "path": request["policy"]["trust"][policy_key]["path"],
                "before_store_id": current_id,
                "after_store_id": proposed_id,
                "retired_signer_ids": list(request["retire_signer_ids"]),
                "active_signer_ids": sorted(item["signer_id"] for item in active),
                "status_index": status_index_change,
            },
            proposed_id != current_id,
        )

    def _boot_change(
        self, request: Mapping[str, Any], before: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        if request["kind"] != "boot-settings":
            return None, False
        installed_path = _under(
            self.root, Path(request["policy"]["boot"]["installed_profile_path"])
        )
        installed = load_boot_profile(installed_path, self.registry)
        desired = request["boot_profile"]
        if before.get("boot_profile_id") != installed["profile_id"]:
            raise HostMaintenanceChanged(
                "installed boot profile changed during maintenance planning"
            )
        inspection = before.get("boot")
        if not isinstance(inspection, Mapping):
            raise HostMaintenanceError("boot-setting maintenance lacks boot inspection")
        firmware = inspection.get("firmware")
        directives = (
            firmware.get("directives", []) if isinstance(firmware, Mapping) else []
        )
        drift = list(inspection.get("drift", []))
        unowned_drift = [
            item
            for item in drift
            if not item.startswith("managed firmware setting ")
            and not item.startswith("required device-tree overlays are absent:")
        ]
        if unowned_drift:
            raise HostMaintenanceError(
                "boot drift outside the III-managed settings/overlay block requires "
                "physical SD repair or deterministic reprovisioning"
            )
        observed_sources = set()
        for item in directives:
            source = item.get("source") if isinstance(item, Mapping) else None
            path = Path(source) if isinstance(source, str) else Path()
            if (
                not path.is_absolute()
                or path.parts[:3] != ("/", "boot", "firmware")
                or any(part in {"", ".", ".."} for part in path.parts[1:])
            ):
                raise HostMaintenanceError(
                    "boot inspection contains an unsafe firmware source"
                )
            observed_sources.add(source)
        config_sources = sorted(
            observed_sources | {request["policy"]["boot"]["config_path"]}
        )
        backup_paths = list(request["policy"]["boot"]["backup_paths"])
        backups = []
        for item in backup_paths:
            path = _under(self.root, Path(item))
            backups.append(
                {
                    "path": item,
                    "sha256": _sha256(path),
                    "mode": f"{stat.S_IMODE(path.stat(follow_symlinks=False).st_mode):04o}",
                }
            )
        old_settings = installed["firmware"]["managed_settings"]
        new_settings = desired["firmware"]["managed_settings"]
        setting_deltas = [
            {
                "setting": key,
                "before": old_settings.get(key),
                "after": new_settings.get(key),
            }
            for key in sorted(set(old_settings) | set(new_settings))
            if old_settings.get(key) != new_settings.get(key)
        ]
        old_overlays = installed["firmware"]["managed_overlays"]
        new_overlays = desired["firmware"]["managed_overlays"]
        changed = (
            installed["profile_id"] != desired["profile_id"]
            or inspection.get("accepted") is not True
        )
        return (
            {
                "before_profile_id": installed["profile_id"],
                "after_profile_id": desired["profile_id"],
                "setting_deltas": setting_deltas,
                "overlays_before": list(old_overlays),
                "overlays_after": list(new_overlays),
                "drift_before": drift,
                "config_sources": config_sources,
                "backup_files": backups,
            },
            changed,
        )

    def plan(
        self,
        *,
        operation_id: str,
        client_id: str,
        request: Mapping[str, Any],
        live_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = validate_request(request, self.registry)
        installed = self._policy()
        desired = value["policy"]
        stable_policy_fields = (
            "target_class",
            "platform",
            "host_contract",
            "trust",
            "reboot",
            "boot",
            "application_deployment_may_manage_packages",
        )
        if value["kind"] == "packages":
            if any(
                desired[field] != installed[field] for field in stable_policy_fields
            ):
                raise HostMaintenanceError(
                    "in-place package maintenance cannot change the platform or host contract"
                )
        elif desired != installed:
            raise HostMaintenanceError(
                "non-package maintenance cannot smuggle a maintenance-policy change"
            )
        before = dict(self.snapshot_provider())
        self._platform_matches(before, installed)
        if self.active_operator_count() < 1:
            raise HostMaintenanceError("host maintenance lacks a usable operator")
        expected_packages: list[str] = []
        trust_change, trust_changed = self._trust_change(value, before)
        boot_change, boot_changed = self._boot_change(value, before)
        policy_changed = desired != installed
        if value["kind"] == "packages":
            expected_packages = sorted(
                set(self.package_planner(desired, bool(value["offline"])))
            )
            if any(not PACKAGE_NAME.fullmatch(item) for item in expected_packages):
                raise HostMaintenanceError(
                    "package planner returned an invalid package"
                )
        no_change = (
            not expected_packages
            and not trust_changed
            and not boot_changed
            and not policy_changed
        )
        if not no_change:
            backup = value.get("backup")
            if not isinstance(backup, dict) or backup.get(
                "target_state_hash"
            ) != live_state.get("target_state_hash"):
                raise HostMaintenanceError(
                    "a verified backup for the exact current target state is required"
                )
        reboot_prefixes = tuple(installed["reboot"]["package_prefixes"])
        plan: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "maintenance_id": "0" * 64,
            "operation_id": operation_id,
            "client_id": client_id,
            "request": value,
            "before": before,
            "installed_policy_id": installed["policy_id"],
            "playbook_sha256": self._playbook_sha256(),
            "executor_sha256": self._executor_sha256(),
            "expected_package_changes": expected_packages,
            "trust_change": trust_change,
            "boot_change": boot_change,
            "mutations": (
                []
                if no_change
                else (
                    [
                        "converge governed packages from fixed signed snapshots",
                        "retain before/after package and platform evidence",
                    ]
                    if value["kind"] == "packages"
                    else (
                        [
                            "back up governed boot files before mutation",
                            "converge only the retained boot profile block",
                            "retain before/after boot settings and hashes",
                        ]
                        if value["kind"] == "boot-settings"
                        else [
                            f"replace only {value['kind']} public trust",
                            "retain prior trust and commissioning-invalidating evidence",
                        ]
                    )
                )
            ),
            "required_checks": [
                "receiver target-wide lease and single-use nonce",
                "maintenance-safe state immediately before mutation",
                "state-bound verified backup before material change",
                "protected qualified recovery release after mutation/reboot",
            ],
            "declared_permissions": (
                []
                if no_change
                else (
                    ["root package and repository convergence"]
                    if value["kind"] == "packages"
                    else (
                        ["write governed boot profile and boot files"]
                        if value["kind"] == "boot-settings"
                        else [f"replace {value['kind']} public trust store"]
                    )
                )
            ),
            "reboot_expected": boot_changed
            or any(name.startswith(reboot_prefixes) for name in expected_packages),
            "no_change": no_change,
        }
        if trust_change is not None:
            plan["mutations"].append(
                f"retire {len(trust_change['retired_signer_ids'])} signer(s)"
            )
        plan["maintenance_id"] = _identity(plan, "maintenance_id")
        self.registry.validate("host-maintenance-plan", plan)
        return plan

    def _transaction(self) -> dict[str, Any] | None:
        if not self.current_path.exists() and not self.current_path.is_symlink():
            return None
        value = _object(
            self.current_path, label="host-maintenance transaction", canonical=True
        )
        self.registry.validate("host-maintenance-transaction", value)
        if value.get("transaction_id") != _identity(value, "transaction_id"):
            raise HostMaintenanceError("host-maintenance transaction identity mismatch")
        return value

    def _save_transaction(self, value: dict[str, Any]) -> dict[str, Any]:
        value["transaction_id"] = _identity(value, "transaction_id")
        self.registry.validate("host-maintenance-transaction", value)
        atomic_document(self.current_path, value, mode=0o640)
        archive = self.state_root / f"{value['maintenance_id']}.json"
        atomic_document(archive, value, mode=0o640)
        return value

    def _new_transaction(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": TRANSACTION_SCHEMA,
            "transaction_id": "0" * 64,
            "maintenance_id": plan["maintenance_id"],
            "operation_id": plan["operation_id"],
            "client_id": plan["client_id"],
            "kind": plan["request"]["kind"],
            "phase": "applying",
            "before": dict(plan["before"]),
            "after": None,
            "changed_packages": [],
            "trust_change": None,
            "boot_change": plan["boot_change"],
            "reboot": {
                "required": False,
                "scheduled": False,
                "before_boot_id": plan["before"]["platform"]["boot_id"],
                "after_boot_id": None,
            },
            "protected_release_validation": None,
            "commissioning": {"state": "unchanged", "reasons": []},
            "failure": None,
        }

    def assert_mutation_allowed(self, action: str) -> None:
        current = self._transaction()
        if current is None or current["phase"] in {"completed", "failed"}:
            return
        if action == "host-reboot" and current["phase"] in {
            "reboot-required",
            "reboot-scheduled",
        }:
            return
        raise HostMaintenanceError(
            f"host maintenance {current['maintenance_id']} is {current['phase']}"
        )

    def _default_ansible_runner(
        self, argv: Sequence[str], environment: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if self.root == Path("/"):
            try:
                extra_vars = argv[argv.index("--extra-vars") + 1]
            except (ValueError, IndexError) as exc:
                raise HostMaintenanceError(
                    "fixed Ansible invocation lacks retained extra variables"
                ) from exc
            if not extra_vars.startswith("@"):
                raise HostMaintenanceError(
                    "fixed Ansible invocation has an unsafe extra-variable source"
                )
            extra_path = Path(extra_vars[1:])
            maintenance_id = extra_path.parent.name
            expected = self.state_root / maintenance_id / "ansible-extra-vars.json"
            if (
                not HASH.fullmatch(maintenance_id)
                or extra_path != expected
                or argv[-1] != str(self.playbook_path)
            ):
                raise HostMaintenanceError(
                    "fixed Ansible invocation escapes its retained transaction"
                )
            unit = f"iii-host-maintenance@{maintenance_id}.service"
            completed = subprocess.run(
                ["/usr/bin/systemctl", "start", unit],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=7200,
                env=dict(environment),
            )
            if completed.returncode != 0:
                journal = subprocess.run(
                    [
                        "/usr/bin/journalctl",
                        "--unit",
                        unit,
                        "--lines",
                        "20",
                        "--no-pager",
                    ],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                    env=dict(environment),
                )
                return subprocess.CompletedProcess(
                    completed.args,
                    completed.returncode,
                    stdout=completed.stdout + journal.stdout,
                )
            return completed
        return subprocess.run(
            list(argv),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=7200,
            env=dict(environment),
        )

    def _run_ansible(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        transaction_root = self.state_root / plan["maintenance_id"]
        transaction_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        extra_vars = transaction_root / "ansible-extra-vars.json"
        result_path = transaction_root / "ansible-result.json"
        request = plan["request"]
        kind = request["kind"]
        trust_path = ""
        if kind in TRUST_KINDS:
            policy_key, _authority = TRUST_KINDS[kind]
            trust_path = request["policy"]["trust"][policy_key]["path"]
        payload = {
            "iii_maintenance_kind": kind,
            "iii_maintenance_policy": request["policy"],
            "iii_maintenance_offline": request["offline"],
            "iii_maintenance_trust_path": trust_path,
            "iii_maintenance_trust_store": request["trust_store"] or {},
            "iii_maintenance_release_status_index": request.get("release_status_index")
            or {},
            "iii_maintenance_boot_profile": request.get("boot_profile") or {},
            "iii_maintenance_result_path": str(result_path),
        }
        atomic_bytes(extra_vars, canonical_json(payload) + b"\n", mode=0o600)
        result = self.ansible_runner(
            [
                "/usr/bin/ansible-playbook",
                "--inventory",
                "localhost,",
                "--limit",
                "localhost",
                "--extra-vars",
                "@" + str(extra_vars),
                str(self.playbook_path),
            ],
            {**os.environ, "ANSIBLE_NOCOLOR": "1"},
        )
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            output = str(getattr(result, "stdout", ""))
            raise HostMaintenanceRecoveryRequired(
                "fixed Ansible maintenance failed; rerun idempotently or reimage: "
                + "\n".join(output.splitlines()[-20:])
            )
        if not result_path.exists():
            raise HostMaintenanceRecoveryRequired(
                "host-maintenance Ansible result is missing"
            )
        try:
            recap = _object(
                result_path,
                label="host-maintenance Ansible result",
                canonical=True,
            )
            self.registry.validate("host-maintenance-ansible-result", recap)
        except ContractError as exc:
            raise HostMaintenanceRecoveryRequired(
                f"host-maintenance Ansible result is invalid: {exc}"
            ) from exc
        if (
            recap["kind"] != kind
            or recap["policy_id"] != request["policy"]["policy_id"]
            or (
                recap["boot_profile_id"]
                != (
                    request["boot_profile"]["profile_id"]
                    if kind == "boot-settings"
                    else None
                )
            )
        ):
            raise HostMaintenanceRecoveryRequired(
                "host-maintenance Ansible result does not match the retained plan"
            )
        return recap

    @staticmethod
    def _package_delta(
        before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> tuple[list[str], list[str]]:
        old = before.get("packages", {})
        new = after.get("packages", {})
        changed = sorted(
            name for name in set(old) | set(new) if old.get(name) != new.get(name)
        )
        removed = sorted(name for name in old if name not in new)
        return changed, removed

    def _refresh_host_report(
        self, policy: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        path = _under(self.root, HOST_REPORT_PATH)
        report = _object(path, label="host baseline report", canonical=True)
        if report.get("schema") != "iii.host-baseline-report/v1":
            raise HostMaintenanceRecoveryRequired("host baseline report is invalid")
        report["packages"] = {
            package.split("=", 1)[0]: snapshot["packages"].get(
                package.split("=", 1)[0], []
            )
            for package in policy["governed_packages"]
        }
        atomic_document(path, report, mode=0o640)

    def _commissioning_marker(
        self, transaction: Mapping[str, Any], reasons: Sequence[str]
    ) -> dict[str, Any]:
        if not reasons:
            return {"state": "unchanged", "reasons": []}
        marker: dict[str, Any] = {
            "schema": "iii.recommission-required/v1",
            "marker_id": "0" * 64,
            "maintenance_id": transaction["maintenance_id"],
            "state": "recommission_required",
            "reasons": sorted(set(reasons)),
        }
        marker["marker_id"] = _identity(marker, "marker_id")
        atomic_document(_under(self.root, RECOMMISSION_PATH), marker, mode=0o640)
        return {"state": marker["state"], "reasons": marker["reasons"]}

    def _validate_protected_release(self) -> Mapping[str, Any]:
        state = _object(
            _under(self.root, RELEASE_STATE_PATH),
            label="onboard release state",
            canonical=True,
        )
        anchor = state.get("qualified_anchor_release_id")
        releases = state.get("releases")
        if not isinstance(anchor, str) or not isinstance(releases, dict):
            raise HostMaintenanceRecoveryRequired(
                "no protected qualified recovery release is available; reprovision"
            )
        record = releases.get(anchor)
        if (
            not isinstance(record, dict)
            or record.get("release_class") != "qualified"
            or record.get("status") != "qualified"
        ):
            raise HostMaintenanceRecoveryRequired(
                "protected recovery release is withdrawn, unsafe, or invalid"
            )
        release = _under(self.root, RELEASE_ROOT) / anchor
        receipt = _object(
            release / "manifest.json", label="staged release receipt", canonical=True
        )
        manifest = _object(
            release / "release-manifest.json",
            label="protected release manifest",
            canonical=True,
        )
        report = _object(
            _under(self.root, HOST_REPORT_PATH),
            label="host baseline report",
            canonical=True,
        )
        target = manifest.get("target", {})
        if (
            receipt.get("release_id") != anchor
            or manifest.get("release_id") != anchor
            or target.get("host_baseline") != report.get("baseline_id")
            or target.get("host_unit_contract") != report.get("unit_contract_id")
            or target.get("definition_id") != report.get("target_definition_id")
        ):
            raise HostMaintenanceRecoveryRequired(
                "protected qualified release does not validate against the maintained host"
            )
        value = {
            "schema": "iii.host-maintenance-recovery-validation/v1",
            "validation_id": "0" * 64,
            "release_id": anchor,
            "release_status": record["status"],
            "host_baseline": report["baseline_id"],
            "valid": True,
        }
        value["validation_id"] = _identity(value, "validation_id")
        return value

    def apply(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        self.registry.validate("host-maintenance-plan", plan)
        if plan.get("maintenance_id") != _identity(plan, "maintenance_id"):
            raise HostMaintenanceChanged("host-maintenance plan identity changed")
        self.assert_mutation_allowed("host-maintenance")
        if self._policy()["policy_id"] != plan["installed_policy_id"]:
            raise HostMaintenanceChanged("installed policy changed after planning")
        if self._playbook_sha256() != plan["playbook_sha256"]:
            raise HostMaintenanceChanged("installed playbook changed after planning")
        if self._executor_sha256() != plan["executor_sha256"]:
            raise HostMaintenanceChanged("installed executor changed after planning")
        before = dict(self.snapshot_provider())
        if before.get("snapshot_id") != plan["before"].get("snapshot_id"):
            raise HostMaintenanceChanged(
                "host package/platform state changed after planning"
            )
        transaction = self._new_transaction(plan)
        self._save_transaction(transaction)
        stopped = False
        trust_backups: list[tuple[Path, bytes]] = []
        boot_backups: list[tuple[Path, bytes, int]] = []
        try:
            if plan["no_change"]:
                proof = dict(self.recovery_validator())
                transaction["after"] = before
                transaction["protected_release_validation"] = proof
                transaction["phase"] = "completed"
                return self._save_transaction(transaction)
            if not self.maintenance_safe():
                raise HostMaintenanceError(
                    "host maintenance requires fresh landed/disarmed owner-free state"
                )
            self.stop_runtime()
            stopped = True
            if plan["request"]["kind"] in TRUST_KINDS:
                policy_key, _authority = TRUST_KINDS[plan["request"]["kind"]]
                trust_path = _under(
                    self.root,
                    Path(plan["request"]["policy"]["trust"][policy_key]["path"]),
                )
                trust_backups.append((trust_path, trust_path.read_bytes()))
                backup_path = (
                    self.state_root / plan["maintenance_id"] / "trust-before.json"
                )
                atomic_bytes(backup_path, trust_backups[0][1], mode=0o600)
                if plan["request"]["kind"] == "release-status-trust":
                    index_path = _under(self.root, STATUS_INDEX_PATH)
                    trust_backups.append((index_path, index_path.read_bytes()))
                    atomic_bytes(
                        self.state_root
                        / plan["maintenance_id"]
                        / "release-status-index-before.json",
                        trust_backups[1][1],
                        mode=0o600,
                    )
            if plan["request"]["kind"] == "boot-settings":
                backup_root = self.state_root / plan["maintenance_id"] / "boot-before"
                backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                for index, item in enumerate(plan["boot_change"]["backup_files"]):
                    source = _under(self.root, Path(item["path"]))
                    raw = source.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                        raise HostMaintenanceChanged(
                            "boot file changed after maintenance planning"
                        )
                    boot_backups.append((source, raw, int(item["mode"], 8)))
                    atomic_bytes(
                        backup_root / f"{index:02d}-{source.name}", raw, mode=0o600
                    )
            self._run_ansible(plan)
            if self._policy() != plan["request"]["policy"]:
                raise HostMaintenanceRecoveryRequired(
                    "Ansible did not install the exact retained maintenance policy"
                )
            interim = dict(self.snapshot_provider())
            if plan["request"]["kind"] == "packages":
                self._refresh_host_report(plan["request"]["policy"], interim)
                after = dict(self.snapshot_provider())
            else:
                after = interim
            changed, removed = self._package_delta(before, after)
            if removed:
                raise HostMaintenanceRecoveryRequired(
                    "maintenance unexpectedly removed packages: " + ", ".join(removed)
                )
            if (
                plan["request"]["kind"] == "packages"
                and changed != plan["expected_package_changes"]
            ):
                raise HostMaintenanceRecoveryRequired(
                    "Ansible package delta differs from the retained simulation: "
                    f"expected {plan['expected_package_changes']}, observed {changed}"
                )
            if plan["request"]["kind"] != "packages" and changed:
                raise HostMaintenanceRecoveryRequired(
                    "non-package maintenance unexpectedly changed host packages"
                )
            transaction["after"] = after
            transaction["changed_packages"] = changed
            commissioning_reasons = []
            if plan["request"]["kind"] in TRUST_KINDS:
                trust_change = plan["trust_change"]
                if (
                    trust_change is None
                    or after["trust_store_ids"].get(plan["request"]["kind"])
                    != trust_change["after_store_id"]
                ):
                    raise HostMaintenanceRecoveryRequired(
                        "Ansible did not apply the exact retained trust rotation"
                    )
                transaction["trust_change"] = trust_change
                if plan["request"]["kind"] == "release-status-trust" and (
                    after.get("release_status_index_id")
                    != trust_change["status_index"]["after_index_id"]
                ):
                    raise HostMaintenanceRecoveryRequired(
                        "Ansible did not apply the exact replacement release-status index"
                    )
                commissioning_reasons.append(plan["request"]["kind"])
            if plan["request"]["kind"] == "boot-settings":
                boot_change = plan["boot_change"]
                if (
                    after.get("boot_profile_id") != boot_change["after_profile_id"]
                    or not isinstance(after.get("boot"), Mapping)
                    or after["boot"].get("accepted") is not True
                ):
                    raise HostMaintenanceRecoveryRequired(
                        "Ansible boot convergence differs from the retained profile"
                    )
                transaction["boot_change"] = boot_change
                commissioning_reasons.append("boot-settings")
            reboot_required = bool(
                after.get("reboot_required") or plan["reboot_expected"]
            )
            transaction["reboot"]["required"] = reboot_required
            transaction["commissioning"] = self._commissioning_marker(
                transaction, commissioning_reasons
            )
            if reboot_required:
                transaction["phase"] = "reboot-required"
                return self._save_transaction(transaction)
            transaction["protected_release_validation"] = dict(
                self.recovery_validator()
            )
            transaction["phase"] = "completed"
            self.resume_runtime()
            stopped = False
            return self._save_transaction(transaction)
        except Exception as exc:
            for backup_path, backup_raw, backup_mode in reversed(boot_backups):
                atomic_bytes(backup_path, backup_raw, mode=backup_mode)
            for backup_path, backup_raw in reversed(trust_backups):
                atomic_bytes(backup_path, backup_raw, mode=0o640)
            transaction["phase"] = "failed"
            transaction["failure"] = {
                "code": getattr(exc, "code", "III_HOST_MAINTENANCE_FAILED"),
                "message": str(exc),
                "recommendation": (
                    "inspect the retained transaction and retry the fixed Ansible plan; "
                    "restore the protected release or reprovision if validation cannot pass"
                ),
            }
            transaction = self._save_transaction(transaction)
            if stopped:
                try:
                    self.resume_runtime()
                except Exception:
                    pass
            raise

    def plan_reboot(
        self, *, operation_id: str, client_id: str, maintenance_id: str
    ) -> dict[str, Any]:
        current = self._transaction()
        if (
            current is None
            or current.get("maintenance_id") != maintenance_id
            or current.get("phase") != "reboot-required"
        ):
            raise HostMaintenanceError(
                "explicit reboot requires the exact reboot-required maintenance transaction"
            )
        return {"maintenance_id": maintenance_id}

    def _default_reboot(self) -> Any:
        return subprocess.run(
            ["/usr/bin/systemctl", "reboot"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )

    def schedule_reboot(self, maintenance_id: str) -> dict[str, Any]:
        self.assert_mutation_allowed("host-reboot")
        transaction = self._transaction()
        assert transaction is not None
        if transaction["maintenance_id"] != maintenance_id:
            raise HostMaintenanceError("reboot targets another maintenance transaction")
        if transaction["phase"] == "reboot-required":
            transaction["phase"] = "reboot-scheduled"
            transaction["reboot"]["scheduled"] = True
            saved = self._save_transaction(transaction)
        else:
            saved = transaction
        try:
            self.reboot_host()
        except Exception as exc:
            current = self._transaction()
            if (
                current is not None
                and current["maintenance_id"] == maintenance_id
                and current["phase"] == "reboot-scheduled"
            ):
                current["phase"] = "reboot-required"
                current["reboot"]["scheduled"] = False
                self._save_transaction(current)
            raise HostMaintenanceRecoveryRequired(
                "explicit reboot request failed before restart; retry the retained reboot plan"
            ) from exc
        return saved

    def reconcile(self) -> dict[str, Any]:
        transaction = self._transaction()
        if transaction is None:
            return {"schema": "iii.host-maintenance-reconcile/v1", "state": "none"}
        phase = transaction["phase"]
        if phase == "applying":
            transaction["phase"] = "failed"
            transaction["failure"] = {
                "code": "III_HOST_MAINTENANCE_INTERRUPTED",
                "message": "host restarted or receiver exited during Ansible maintenance",
                "recommendation": "inspect evidence and rerun the idempotent retained policy or reprovision",
            }
            self._save_transaction(transaction)
        elif phase == "reboot-scheduled":
            after = dict(self.snapshot_provider())
            current_boot = after["platform"]["boot_id"]
            if current_boot != transaction["reboot"]["before_boot_id"]:
                transaction["phase"] = "validating"
                transaction["after"] = after
                transaction["reboot"]["after_boot_id"] = current_boot
                transaction = self._save_transaction(transaction)
                try:
                    boot_change = transaction.get("boot_change")
                    if boot_change is not None and (
                        after.get("boot_profile_id") != boot_change["after_profile_id"]
                        or not isinstance(after.get("boot"), Mapping)
                        or after["boot"].get("accepted") is not True
                    ):
                        raise HostMaintenanceRecoveryRequired(
                            "post-boot configuration differs from the retained boot profile"
                        )
                    transaction["protected_release_validation"] = dict(
                        self.recovery_validator()
                    )
                    transaction["phase"] = "completed"
                    transaction["failure"] = None
                except Exception as exc:
                    transaction["phase"] = "failed"
                    transaction["failure"] = {
                        "code": getattr(
                            exc,
                            "code",
                            "III_HOST_MAINTENANCE_POSTBOOT_VALIDATION_FAILED",
                        ),
                        "message": str(exc),
                        "recommendation": (
                            "recover the protected qualified release through the receiver; "
                            "reprovision when its host contract cannot be restored"
                        ),
                    }
                transaction = self._save_transaction(transaction)
        return {
            "schema": "iii.host-maintenance-reconcile/v1",
            "maintenance_id": transaction["maintenance_id"],
            "state": transaction["phase"],
            "transaction_id": transaction["transaction_id"],
        }

    def status(self) -> dict[str, Any]:
        transaction = self._transaction()
        return {
            "schema": "iii.host-maintenance-status/v1",
            "transaction": transaction,
            "mutation_blocked": bool(
                transaction and transaction["phase"] not in {"completed", "failed"}
            ),
            "recovery_recommendation": (
                None
                if transaction is None or transaction["phase"] != "failed"
                else transaction["failure"].get("recommendation")
            ),
        }


__all__ = [
    "HostMaintenanceChanged",
    "HostMaintenanceController",
    "HostMaintenanceError",
    "HostMaintenanceRecoveryRequired",
    "build_request",
    "validate_policy",
    "validate_request",
]
