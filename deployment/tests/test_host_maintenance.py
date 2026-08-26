from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from iii_deployment.contracts import ContractRegistry, canonical_json, content_identity
from iii_deployment.host_maintenance import (
    FIXED_PLATFORM,
    HostMaintenanceChanged,
    HostMaintenanceController,
    HostMaintenanceError,
    HostMaintenanceRecoveryRequired,
)
from iii_deployment.release_status import (
    create_status_index,
    create_status_statement,
    verify_status_index,
)
from iii_deployment.signers import (
    add_trusted_signer,
    generate_signer,
    load_trusted_signers,
    signer_proof,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
HASH = "a" * 64
CLIENT = "b" * 64
TARGET_STATE = "c" * 64


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _identified(value: dict, field: str) -> dict:
    value[field] = content_identity(
        {key: item for key, item in value.items() if key != field}
    )
    return value


def _signer(tmp_path: Path, name: str, authority: str) -> tuple[Path, dict]:
    private = tmp_path / f"{name}.pem"
    public = tmp_path / f"{name}.json"
    descriptor = generate_signer(
        private, public, authority=authority, registry=REGISTRY
    )
    return private, descriptor


class MaintenanceCase:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "target"
        self.policy = json.loads(
            (
                ROOT / "deployment/host-maintenance/host-maintenance-policy.json"
            ).read_text()
        )
        _write(self.root / "etc/iii/host-maintenance-policy.json", self.policy)
        playbook = self.root / "usr/share/iii/host-maintenance/aircraft-maintenance.yml"
        playbook.parent.mkdir(parents=True)
        playbook.write_text("---\n- hosts: localhost\n", encoding="utf-8")
        executor = self.root / "etc/systemd/system/iii-host-maintenance@.service"
        executor.parent.mkdir(parents=True)
        executor.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
        self.bundle_key, self.bundle_descriptor = _signer(
            tmp_path, "bundle", "ci-qualified"
        )
        self.status_key, self.status_descriptor = _signer(
            tmp_path, "status", "release-status"
        )
        self.bundle_store_path = self.root / "etc/iii/trust/bundle-signers.json"
        self.status_store_path = self.root / "etc/iii/trust/release-status-signers.json"
        self.bundle_store_path.parent.mkdir(parents=True)
        add_trusted_signer(
            self.bundle_store_path,
            tmp_path / "bundle.json",
            signer_proof(self.bundle_key),
            REGISTRY,
        )
        add_trusted_signer(
            self.status_store_path,
            tmp_path / "status.json",
            signer_proof(self.status_key),
            REGISTRY,
        )
        statement = create_status_statement(
            operation_id="status-initial-test",
            release_id="d" * 64,
            version="v1.2.3",
            status="qualified",
            reason="qualified fixture",
            superseding_version=None,
            recorded_at="2026-08-27T00:00:00Z",
            private_key_path=self.status_key,
            registry=REGISTRY,
            previous_global=None,
            previous_release=None,
        )
        self.status_index = create_status_index(
            [statement],
            generated_at="2026-08-27T00:00:00Z",
            private_key_path=self.status_key,
            trusted_signers=load_trusted_signers(self.status_store_path, REGISTRY),
            registry=REGISTRY,
        )
        _write(
            self.root / "var/lib/iii/deployment/release-status-index.json",
            self.status_index,
        )
        _write(
            self.root / "var/lib/iii/deployment/host-baseline-report.json",
            {
                "schema": "iii.host-baseline-report/v1",
                "baseline_id": self.policy["host_contract"]["baseline_id"],
                "unit_contract_id": self.policy["host_contract"]["unit_contract_id"],
                "target_definition_id": self.policy["host_contract"][
                    "target_definition_id"
                ],
                "shared_target_profile_id": self.policy["host_contract"][
                    "shared_target_profile_id"
                ],
                "packages": {},
            },
        )
        self.packages = {"curl": ["1"]}
        self.boot_id = "boot-before"
        self.reboot_required = False
        self.planned_packages: list[str] = []
        self.ansible_calls = 0
        self.stops = 0
        self.resumes = 0
        self.reboots = 0
        self.recovery_failure: Exception | None = None
        self.ansible_failure: Exception | None = None

    def snapshot(self) -> dict:
        bundle = load_trusted_signers(self.bundle_store_path, REGISTRY)
        status = load_trusted_signers(self.status_store_path, REGISTRY)
        index = json.loads(
            (self.root / "var/lib/iii/deployment/release-status-index.json").read_text()
        )
        value = {
            "schema": "iii.host-maintenance-snapshot/v1",
            "snapshot_id": "0" * 64,
            "platform": {**FIXED_PLATFORM, "kernel": "6.8.0", "boot_id": self.boot_id},
            "host_contract": copy.deepcopy(self.policy["host_contract"]),
            "packages": copy.deepcopy(self.packages),
            "trust_store_ids": {
                "bundle-trust": content_identity(bundle),
                "release-status-trust": content_identity(status),
            },
            "release_status_index_id": index["index_id"],
            "reboot_required": self.reboot_required,
        }
        return _identified(value, "snapshot_id")

    def recovery(self) -> dict:
        if self.recovery_failure is not None:
            raise self.recovery_failure
        return {
            "schema": "iii.host-maintenance-recovery-validation/v1",
            "validation_id": "e" * 64,
            "release_id": "d" * 64,
            "valid": True,
        }

    def retain_ansible_result(self, argv) -> None:
        extra_vars = Path(argv[argv.index("--extra-vars") + 1].removeprefix("@"))
        payload = json.loads(extra_vars.read_text())
        _write(
            Path(payload["iii_maintenance_result_path"]),
            {
                "schema": "iii.host-maintenance-ansible-result/v1",
                "kind": payload["iii_maintenance_kind"],
                "policy_id": payload["iii_maintenance_policy"]["policy_id"],
                "packages": {
                    name: [{"version": version} for version in versions]
                    for name, versions in self.packages.items()
                },
                "reboot_required": self.reboot_required,
            },
        )

    def ansible(self, argv, _environment):
        self.ansible_calls += 1
        if self.ansible_failure is not None:
            raise self.ansible_failure
        self.retain_ansible_result(argv)
        return SimpleNamespace(returncode=0, stdout="ok")

    def controller(self, *, operators: int = 1) -> HostMaintenanceController:
        return HostMaintenanceController(
            root=self.root,
            registry=REGISTRY,
            maintenance_safe=lambda: True,
            stop_runtime=lambda: setattr(self, "stops", self.stops + 1) or (),
            resume_runtime=lambda: setattr(self, "resumes", self.resumes + 1),
            reboot=lambda: setattr(self, "reboots", self.reboots + 1),
            snapshot_provider=self.snapshot,
            package_planner=lambda _packages, _offline: list(self.planned_packages),
            ansible_runner=self.ansible,
            recovery_validator=self.recovery,
            active_operator_count=lambda: operators,
        )

    def request(
        self,
        *,
        kind: str = "packages",
        policy: dict | None = None,
        offline: bool = False,
        backup: bool = False,
        trust_store: dict | None = None,
        status_index: dict | None = None,
        retired: tuple[str, ...] = (),
        proofs: tuple[dict, ...] = (),
    ) -> dict:
        value = {
            "schema": "iii.host-maintenance-request/v1",
            "request_id": "0" * 64,
            "kind": kind,
            "policy": copy.deepcopy(policy or self.policy),
            "offline": offline,
            "backup": (
                {
                    "schema": "iii.host-backup-receipt/v1",
                    "backup_id": "f" * 64,
                    "target_state_hash": TARGET_STATE,
                    "verified": True,
                    "record_sha256": "1" * 64,
                }
                if backup
                else None
            ),
            "trust_store": trust_store,
            "release_status_index": status_index,
            "retire_signer_ids": list(retired),
            "replacement_proofs": list(proofs),
        }
        return _identified(value, "request_id")

    def plan(self, controller: HostMaintenanceController, request: dict) -> dict:
        return controller.plan(
            operation_id="host-maintenance-test",
            client_id=CLIENT,
            request=request,
            live_state={"target_state_hash": TARGET_STATE},
        )


def test_idempotent_no_change_validates_recovery_without_mutation(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()
    plan = case.plan(controller, case.request())

    assert plan["no_change"] is True
    result = controller.apply(plan)

    assert result["phase"] == "completed"
    assert result["before"] == result["after"]
    assert case.ansible_calls == case.stops == case.resumes == 0


def test_policy_matches_provisioned_packages_and_normal_release_forbids_host_mutation() -> (
    None
):
    policy = json.loads(
        (ROOT / "deployment/host-maintenance/host-maintenance-policy.json").read_text()
    )
    variables = yaml.safe_load(
        (ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml").read_text()
    )
    assert policy["governed_packages"] == sorted(
        set(variables["iii_host_packages"] + variables["iii_ros_packages"])
    )
    receiver_policy = json.loads((ROOT / "deployment/receiver-policy.json").read_text())
    forbidden = set(receiver_policy["normal_release_forbidden_paths"])
    assert {
        "/etc/apt/sources.list.d/ubuntu.sources",
        "/etc/apt/sources.list.d/ros2.sources",
        "/etc/iii/host-maintenance-policy.json",
        "/etc/iii/trust",
        "/etc/systemd/system/iii-host-maintenance@.service",
        "/usr/share/iii/host-maintenance",
    } <= forbidden
    target = json.loads(
        (ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json").read_text()
    )
    assert (
        target["release_boundary"]["normal_deployment_may_run_package_manager"] is False
    )
    playbook = (
        ROOT / "deployment/host-maintenance/aircraft-maintenance.yml"
    ).read_text()
    assert playbook.count("Dir::Etc::sourcelist=/dev/null") == 2
    assert playbook.count("Dir::Etc::sourceparts=") == 2
    assert "--no-download" in playbook
    executor = (ROOT / "deployment/systemd/iii-host-maintenance@.service").read_text()
    assert "ProtectSystem=no" in executor
    assert "ansible-playbook" in executor
    assert "%i/ansible-extra-vars.json" in executor


def test_material_change_requires_exact_state_bound_backup(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    case.planned_packages = ["curl"]

    with pytest.raises(HostMaintenanceError, match="verified backup"):
        case.plan(case.controller(), case.request())


def test_executor_drift_after_planning_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()
    plan = case.plan(controller, case.request())
    executor = case.root / "etc/systemd/system/iii-host-maintenance@.service"
    executor.write_text("[Service]\nType=oneshot\n# drift\n", encoding="utf-8")

    with pytest.raises(HostMaintenanceChanged, match="executor changed"):
        controller.apply(plan)

    assert case.stops == case.ansible_calls == 0


def test_package_update_retains_report_and_requires_explicit_reboot(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    case.planned_packages = ["curl"]
    case.reboot_required = False
    controller = case.controller()

    def ansible(argv, _environment):
        case.ansible_calls += 1
        case.packages["curl"] = ["2"]
        case.reboot_required = True
        case.retain_ansible_result(argv)
        return SimpleNamespace(returncode=0, stdout="ok")

    controller.ansible_runner = ansible
    result = controller.apply(case.plan(controller, case.request(backup=True)))

    assert result["phase"] == "reboot-required"
    assert result["changed_packages"] == ["curl"]
    assert result["before"]["packages"]["curl"] == ["1"]
    assert result["after"]["packages"]["curl"] == ["2"]
    assert case.reboots == 0
    reboot_plan = controller.plan_reboot(
        operation_id="host-reboot-test",
        client_id=CLIENT,
        maintenance_id=result["maintenance_id"],
    )
    controller.schedule_reboot(reboot_plan["maintenance_id"])
    assert case.reboots == 1
    case.boot_id = "boot-after"
    reconciled = controller.reconcile()
    assert reconciled["state"] == "completed"
    assert controller.status()["transaction"]["reboot"]["after_boot_id"] == "boot-after"


def test_interrupted_ansible_is_failed_and_recoverable(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    case.planned_packages = ["curl"]
    controller = case.controller()
    controller.ansible_runner = lambda *_args: SimpleNamespace(
        returncode=2, stdout="fatal: simulated interrupted transaction"
    )
    plan = case.plan(controller, case.request(backup=True))

    with pytest.raises(HostMaintenanceRecoveryRequired, match="fixed Ansible"):
        controller.apply(plan)

    status = controller.status()
    assert status["transaction"]["phase"] == "failed"
    assert "reprovision" in status["recovery_recommendation"]
    assert case.resumes == 1


def test_postboot_validation_failure_is_retained(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    case.planned_packages = ["curl"]
    controller = case.controller()

    def ansible(argv, _environment):
        case.packages["curl"] = ["2"]
        case.reboot_required = True
        case.retain_ansible_result(argv)
        return SimpleNamespace(returncode=0, stdout="ok")

    controller.ansible_runner = ansible
    result = controller.apply(case.plan(controller, case.request(backup=True)))
    controller.schedule_reboot(result["maintenance_id"])
    case.boot_id = "boot-after"
    case.recovery_failure = HostMaintenanceRecoveryRequired("anchor invalid")

    reconciled = controller.reconcile()

    assert reconciled["state"] == "failed"
    assert (
        "protected qualified release" in controller.status()["recovery_recommendation"]
    )


def test_failed_explicit_reboot_can_be_retried_without_stranding_transaction(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    case.planned_packages = ["curl"]
    controller = case.controller()

    def ansible(argv, _environment):
        case.packages["curl"] = ["2"]
        case.reboot_required = True
        case.retain_ansible_result(argv)
        return SimpleNamespace(returncode=0, stdout="ok")

    controller.ansible_runner = ansible
    result = controller.apply(case.plan(controller, case.request(backup=True)))
    controller.reboot_host = lambda: (_ for _ in ()).throw(OSError("inhibited"))

    with pytest.raises(HostMaintenanceRecoveryRequired, match="reboot request failed"):
        controller.schedule_reboot(result["maintenance_id"])

    retained = controller.status()["transaction"]
    assert retained["phase"] == "reboot-required"
    assert retained["reboot"]["scheduled"] is False


def test_major_platform_or_ros_transition_is_rejected(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    desired = copy.deepcopy(case.policy)
    desired["platform"]["release"] = "26.04"
    _identified(desired, "policy_id")
    request = case.request(policy=desired)

    with pytest.raises(HostMaintenanceError, match="major Ubuntu or ROS"):
        case.plan(case.controller(), request)


def test_offline_cache_is_checked_before_mutation(monkeypatch, tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if "--simulate" in command:
            return SimpleNamespace(returncode=0, stdout="Inst curl (2 repo [arm64])\n")
        return SimpleNamespace(returncode=100, stdout="cache miss")

    monkeypatch.setattr("iii_deployment.host_maintenance.subprocess.run", run)
    with pytest.raises(HostMaintenanceError, match="cache is incomplete"):
        controller._plan_packages(case.policy, True)
    assert len(calls) == 2
    assert all("Dir::Etc::sourcelist=/dev/null" in call for call in calls)
    assert "--no-download" in calls[1]
    assert case.stops == case.ansible_calls == 0


def test_cached_offline_package_plan_is_supported(monkeypatch, tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()

    def run(command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Inst curl (2 repo [arm64])\n"
                if "--simulate" in command
                else "cached\n"
            ),
        )

    monkeypatch.setattr("iii_deployment.host_maintenance.subprocess.run", run)
    assert controller._plan_packages(case.policy, True) == ["curl"]


def test_offline_plan_rejects_snapshot_selection_change_before_apt(
    monkeypatch, tmp_path: Path
) -> None:
    case = MaintenanceCase(tmp_path)
    desired = copy.deepcopy(case.policy)
    desired["snapshots"]["ubuntu"] += "/different"
    calls = []
    monkeypatch.setattr(
        "iii_deployment.host_maintenance.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(HostMaintenanceError, match="offline maintenance cannot change"):
        case.controller()._plan_packages(desired, True)

    assert calls == []


def test_unavailable_online_snapshot_or_package_fails_before_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=100, stdout="snapshot unavailable")

    monkeypatch.setattr("iii_deployment.host_maintenance.subprocess.run", run)
    with pytest.raises(HostMaintenanceError, match="snapshots are unavailable"):
        controller._plan_packages(case.policy, False)
    assert calls[0][-1] == "update"
    assert case.stops == case.ansible_calls == 0


def test_unavailable_online_package_fails_after_snapshot_preflight_before_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[-1] == "update":
            return SimpleNamespace(returncode=0, stdout="snapshot ready")
        if "--simulate" in command:
            return SimpleNamespace(returncode=0, stdout="Inst curl (2 repo [arm64])\n")
        return SimpleNamespace(returncode=100, stdout="package unavailable")

    monkeypatch.setattr("iii_deployment.host_maintenance.subprocess.run", run)
    with pytest.raises(HostMaintenanceError, match="governed package is unavailable"):
        controller._plan_packages(case.policy, False)

    assert len(calls) == 3
    assert "--download-only" in calls[-1]
    assert case.stops == case.ansible_calls == 0


def test_successful_ansible_without_retained_recap_fails_closed(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    case.planned_packages = ["curl"]
    controller = case.controller()
    controller.ansible_runner = lambda *_args: SimpleNamespace(
        returncode=0, stdout="recap omitted"
    )

    with pytest.raises(HostMaintenanceRecoveryRequired, match="result is missing"):
        controller.apply(case.plan(controller, case.request(backup=True)))

    assert controller.status()["transaction"]["phase"] == "failed"


def test_production_ansible_dispatch_uses_only_fixed_systemd_instance(
    monkeypatch,
) -> None:
    controller = object.__new__(HostMaintenanceController)
    controller.root = Path("/")
    controller.state_root = Path("/var/lib/iii/deployment/host-maintenance")
    controller.playbook_path = Path(
        "/usr/share/iii/host-maintenance/aircraft-maintenance.yml"
    )
    maintenance_id = "a" * 64
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="unit completed")

    monkeypatch.setattr("iii_deployment.host_maintenance.subprocess.run", run)
    result = controller._default_ansible_runner(
        [
            "/usr/bin/ansible-playbook",
            "--extra-vars",
            "@/var/lib/iii/deployment/host-maintenance/"
            + maintenance_id
            + "/ansible-extra-vars.json",
            str(controller.playbook_path),
        ],
        {"ANSIBLE_NOCOLOR": "1"},
    )

    assert result.returncode == 0
    assert calls[0][0] == [
        "/usr/bin/systemctl",
        "start",
        f"iii-host-maintenance@{maintenance_id}.service",
    ]


def test_production_ansible_dispatch_rejects_unbound_extra_vars(monkeypatch) -> None:
    controller = object.__new__(HostMaintenanceController)
    controller.root = Path("/")
    controller.state_root = Path("/var/lib/iii/deployment/host-maintenance")
    controller.playbook_path = Path(
        "/usr/share/iii/host-maintenance/aircraft-maintenance.yml"
    )
    calls = []
    monkeypatch.setattr(
        "iii_deployment.host_maintenance.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(HostMaintenanceError, match="escapes its retained transaction"):
        controller._default_ansible_runner(
            [
                "/usr/bin/ansible-playbook",
                "--extra-vars",
                "@/tmp/" + "a" * 64 + "/ansible-extra-vars.json",
                str(controller.playbook_path),
            ],
            {},
        )

    assert calls == []


def _rotation_store(case: MaintenanceCase, *, authority: str, status: bool = False):
    current_path = case.status_store_path if status else case.bundle_store_path
    old = load_trusted_signers(current_path, REGISTRY)
    new_key, new_descriptor = _signer(
        case.root.parent,
        "replacement-status" if status else "replacement-bundle",
        authority,
    )
    boundary = {
        "sequence": case.status_index["sequence"],
        "statement_id": case.status_index["statements"][-1]["statement_id"],
    }
    retired = {**old["signers"][0], "state": "revoked"}
    if status:
        retired["trusted_through"] = boundary
    replacement = {
        "signer_id": new_descriptor["signer_id"],
        "algorithm": "Ed25519",
        "authority": authority,
        "public_key": new_descriptor["public_key"],
        "state": "active",
    }
    proposed = {
        "schema_version": "1",
        "store_type": "iii.trusted-signers",
        "signers": sorted([retired, replacement], key=lambda item: item["signer_id"]),
    }
    index = None
    if status:
        index = create_status_index(
            case.status_index["statements"],
            generated_at="2026-08-27T00:01:00Z",
            private_key_path=new_key,
            trusted_signers=proposed,
            registry=REGISTRY,
        )
    return (
        old["signers"][0]["signer_id"],
        proposed,
        index,
        signer_proof(new_key),
    )


def test_bundle_signer_rotation_is_backup_first_and_requires_recommission(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    retired, proposed, _index, proof = _rotation_store(case, authority="ci-qualified")
    controller = case.controller()

    def ansible(argv, _environment):
        _write(case.bundle_store_path, proposed)
        case.retain_ansible_result(argv)
        return SimpleNamespace(returncode=0, stdout="ok")

    controller.ansible_runner = ansible
    request = case.request(
        kind="bundle-trust",
        backup=True,
        trust_store=proposed,
        retired=(retired,),
        proofs=(proof,),
    )
    result = controller.apply(case.plan(controller, request))

    assert result["phase"] == "completed"
    assert result["commissioning"] == {
        "state": "recommission_required",
        "reasons": ["bundle-trust"],
    }
    backup = (
        case.root
        / "var/lib/iii/deployment/host-maintenance"
        / result["maintenance_id"]
        / "trust-before.json"
    )
    assert backup.is_file()
    assert json.loads(backup.read_text())["signers"][0]["state"] == "active"


def test_rotation_cannot_strand_final_signer_or_operator(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    current = load_trusted_signers(case.bundle_store_path, REGISTRY)
    stranded = copy.deepcopy(current)
    stranded["signers"][0]["state"] = "revoked"
    request = case.request(
        kind="bundle-trust",
        backup=True,
        trust_store=stranded,
        retired=(current["signers"][0]["signer_id"],),
        proofs=(),
    )
    with pytest.raises(HostMaintenanceError, match="final signer"):
        case.plan(case.controller(), request)
    with pytest.raises(HostMaintenanceError, match="usable operator"):
        case.plan(case.controller(operators=0), case.request())


def test_rotation_rejects_unproved_replacement_key(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    retired, proposed, _index, _proof = _rotation_store(case, authority="ci-qualified")
    request = case.request(
        kind="bundle-trust",
        backup=True,
        trust_store=proposed,
        retired=(retired,),
    )
    with pytest.raises(HostMaintenanceError, match="proofs must exactly cover"):
        case.plan(case.controller(), request)


def test_rotation_rejects_tampered_replacement_proof(tmp_path: Path) -> None:
    case = MaintenanceCase(tmp_path)
    retired, proposed, _index, proof = _rotation_store(case, authority="ci-qualified")
    replacement = "B" if proof["proof"].startswith("A") else "A"
    tampered = {**proof, "proof": replacement + proof["proof"][1:]}
    request = case.request(
        kind="bundle-trust",
        backup=True,
        trust_store=proposed,
        retired=(retired,),
        proofs=(tampered,),
    )

    with pytest.raises(HostMaintenanceError, match="proof of possession"):
        case.plan(case.controller(), request)


def test_rotation_rejects_mismatched_public_identity_before_mutation(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    retired, proposed, _index, proof = _rotation_store(case, authority="ci-qualified")
    replacement = next(
        item for item in proposed["signers"] if item["state"] == "active"
    )
    replacement["signer_id"] = "a" * 64
    proposed["signers"].sort(key=lambda item: item["signer_id"])
    request = case.request(
        kind="bundle-trust",
        backup=True,
        trust_store=proposed,
        retired=(retired,),
        proofs=({**proof, "signer_id": "a" * 64},),
    )

    with pytest.raises(HostMaintenanceError, match="public-key identity mismatch"):
        case.plan(case.controller(), request)

    assert case.stops == case.ansible_calls == 0


def test_compromised_status_signer_cutover_preserves_exact_history(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    retired, proposed, replacement_index, proof = _rotation_store(
        case, authority="release-status", status=True
    )
    controller = case.controller()

    def ansible(argv, _environment):
        _write(case.status_store_path, proposed)
        _write(
            case.root / "var/lib/iii/deployment/release-status-index.json",
            replacement_index,
        )
        case.retain_ansible_result(argv)
        return SimpleNamespace(returncode=0, stdout="ok")

    controller.ansible_runner = ansible
    request = case.request(
        kind="release-status-trust",
        backup=True,
        trust_store=proposed,
        status_index=replacement_index,
        retired=(retired,),
        proofs=(proof,),
    )
    result = controller.apply(case.plan(controller, request))

    assert result["phase"] == "completed"
    assert result["commissioning"]["state"] == "recommission_required"
    assert replacement_index["statements"] == case.status_index["statements"]
    latest = verify_status_index(replacement_index, proposed, REGISTRY)
    assert latest["d" * 64]["status"] == "qualified"

    rewritten = copy.deepcopy(replacement_index)
    rewritten["statements"][0]["reason"] = "conflicting forged history"
    with pytest.raises(Exception):
        verify_status_index(rewritten, proposed, REGISTRY)


def test_reconcile_marks_interrupted_applying_transaction_failed(
    tmp_path: Path,
) -> None:
    case = MaintenanceCase(tmp_path)
    controller = case.controller()
    plan = case.plan(controller, case.request())
    controller._save_transaction(controller._new_transaction(plan))

    reconciled = controller.reconcile()

    assert reconciled["state"] == "failed"
    assert (
        controller.status()["transaction"]["failure"]["code"]
        == "III_HOST_MAINTENANCE_INTERRUPTED"
    )
