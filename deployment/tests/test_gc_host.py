from __future__ import annotations

import json
import io
import os
from pathlib import Path
import tarfile

import pytest

from iii_deployment.contracts import ContractRegistry, content_identity
from iii_deployment.gc_host import (
    GCHostError,
    _tree_manifest,
    apply_plan,
    build_plan,
    inspect_platform,
    inspect_container_runtime,
    inspect_status,
    load_offline_cache,
    load_policy,
)
from iii import registry as record_registry
import iii_deployment.gc_host as gc_host

ROOT = Path(__file__).parents[2]
DEPLOYMENT = ROOT / "deployment"
SCHEMAS = DEPLOYMENT / "schemas/v1"
POLICY = DEPLOYMENT / "gc-host-policy.json"


def _os_release(path: Path, version: str) -> Path:
    path.write_text(f'ID=ubuntu\nVERSION_ID="{version}"\n', encoding="utf-8")
    return path


def test_policy_pins_both_supported_platforms_and_ros_free_boundaries():
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))

    assert [row["version_id"] for row in policy["supported_platforms"]] == [
        "22.04",
        "24.04",
    ]
    assert policy["automatic_hostname"] == "iii.local"
    assert policy["clock_profiles"] == ["real"]
    assert policy["clock_skip_profiles"] == ["sim"]
    assert "ros-jazzy-*" in policy["forbidden_proxy_host_packages"]
    assert {
        ".config/iii/keys/signing",
        ".local/state/iii/captures",
        ".local/state/iii/logs",
        ".local/state/iii/registry",
        ".local/share/iii/gc-applications",
        ".config/QGroundControl.org",
        ".local/share/QGroundControl",
        "Documents/QGroundControl",
    }.issubset({item["path"] for item in policy["managed_user_paths"]})
    assert "skopeo" in policy["operational_packages"]
    assert policy["container_runtime"] == {
        "default_packages": ["docker.io", "docker-compose-v2"],
        "accepted_existing_provider": "docker-ce",
        "accepted_existing_packages": [
            "containerd.io",
            "docker-buildx-plugin",
            "docker-ce",
            "docker-ce-cli",
            "docker-compose-plugin",
        ],
    }
    assert {
        "gstreamer1.0-gl",
        "gstreamer1.0-libav",
        "gstreamer1.0-plugins-bad",
        "libegl1",
        "libfontconfig1",
        "libgl1",
        "libopengl0",
        "libxcb-cursor0",
        "libxcb-xinerama0",
        "libxkbcommon-x11-0",
    }.issubset(policy["operational_packages"])
    assert set(policy["manual_units"]) == {
        "iii-gc-browser.service",
        "iii-qgc.service",
    }
    assert "iii-gc-px4-parameters.service" in policy["login_units"]
    assert policy["recovery_units"] == ["iii-gc-application-reconcile.service"]
    assert policy["builder"]["definition_sha256"] == (
        __import__("hashlib").sha256((ROOT / "Dockerfile.cc").read_bytes()).hexdigest()
    )


def test_policy_semantics_reject_path_escape_and_proxy_boundary_violation(
    tmp_path, monkeypatch
):
    value = json.loads(POLICY.read_text())
    value["managed_user_paths"][0]["path"] = "../../etc"
    unsafe = tmp_path / "gc-host-policy.json"
    unsafe.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(
        gc_host, "_sha256", lambda _path: value["builder"]["definition_sha256"]
    )
    with pytest.raises(GCHostError, match="path"):
        load_policy(unsafe, ContractRegistry(SCHEMAS))

    value = json.loads(POLICY.read_text())
    value["operational_packages"].append("ros-jazzy-rclcpp")
    unsafe.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(GCHostError, match="ROS/DDS/MAVSDK"):
        load_policy(unsafe, ContractRegistry(SCHEMAS))


@pytest.mark.parametrize(
    ("version", "platform_id"),
    [
        ("22.04", "ubuntu-22.04-x86_64"),
        ("24.04", "ubuntu-24.04-x86_64"),
    ],
)
def test_platform_matrix_reports_the_explicitly_excluded_prerequisites(
    tmp_path, version, platform_id
):
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))

    result = inspect_platform(
        policy,
        os_release_path=_os_release(tmp_path / "os-release", version),
        architecture="amd64",
    )

    assert result["platform_id"] == platform_id
    assert result["architecture"] == "x86_64"
    assert result["graphical_session_required"] is True
    assert set(result["excluded_prerequisites"]) == {
        "disk-partitioning",
        "full-disk-encryption",
        "proprietary-hardware-drivers",
        "ubuntu-installation",
        "vendor-firmware",
    }


def test_stock_ubuntu_os_release_symlink_is_accepted_but_other_links_fail(
    tmp_path, monkeypatch
):
    (tmp_path / "usr/lib").mkdir(parents=True)
    canonical = _os_release(tmp_path / "usr/lib/os-release", "24.04")
    exposed = tmp_path / "etc/os-release"
    exposed.parent.mkdir(parents=True)
    exposed.symlink_to("../usr/lib/os-release")
    monkeypatch.setattr(gc_host, "OS_RELEASE_PATH", exposed)
    monkeypatch.setattr(gc_host, "OS_RELEASE_CANONICAL_PATH", canonical)

    assert gc_host._read_os_release(exposed)["VERSION_ID"] == "24.04"

    other = tmp_path / "etc/other-release"
    other.symlink_to("../usr/lib/os-release")
    with pytest.raises(GCHostError, match="not canonical"):
        gc_host._read_os_release(other)


def test_status_accepts_declared_documents_path_and_normalizes_empty_unit_state(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        gc_host,
        "_current_user",
        lambda *_args, **_kwargs: {
            "name": "gc-test",
            "uid": 1000,
            "gid": 1000,
            "home": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        gc_host,
        "inspect_platform",
        lambda _policy: {
            "platform_id": "ubuntu-24.04-x86_64",
            "os_id": "ubuntu",
            "version_id": "24.04",
            "architecture": "x86_64",
            "graphical_session_required": True,
            "excluded_prerequisites": [
                "disk-partitioning",
                "full-disk-encryption",
                "proprietary-hardware-drivers",
                "ubuntu-installation",
                "vendor-firmware",
            ],
        },
    )

    def missing_unit(*_args, **_kwargs):
        return __import__("subprocess").CompletedProcess(
            [], 0, "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n", ""
        )

    status = inspect_status(
        policy_path=POLICY,
        schema_root=SCHEMAS,
        home=tmp_path,
        runner=missing_unit,
    )

    documents = next(
        item for item in status["paths"] if item["path"] == "Documents/QGroundControl"
    )
    assert documents["exists"] is False
    assert {item["unit_file_state"] for item in status["units"]} == {"unavailable"}


def test_unsupported_os_or_architecture_fails_closed(tmp_path):
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))

    with pytest.raises(GCHostError, match="supports only"):
        inspect_platform(
            policy,
            os_release_path=_os_release(tmp_path / "os-release", "20.04"),
            architecture="x86_64",
        )
    with pytest.raises(GCHostError, match="supports only"):
        inspect_platform(
            policy,
            os_release_path=_os_release(tmp_path / "os-release", "24.04"),
            architecture="aarch64",
        )


def test_container_runtime_preserves_only_a_complete_retained_docker_ce_install():
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))
    packages = policy["container_runtime"]["accepted_existing_packages"]

    def runner(_argv, **_kwargs):
        stdout = "".join(f"{name}\t1.2.3-retained\tii \n" for name in packages)
        return __import__("subprocess").CompletedProcess([], 0, stdout, "")

    result = inspect_container_runtime(policy, runner=runner)

    assert result["provider"] == "docker-ce"
    assert {item["name"] for item in result["existing_packages"]} == set(packages)
    assert "docker.io" not in result["install_packages"]
    assert "docker-compose-v2" not in result["install_packages"]
    assert "skopeo" in result["install_packages"]

    def partial(_argv, **_kwargs):
        return __import__("subprocess").CompletedProcess(
            [], 1, "docker-ce\t1.2.3-retained\tii \n", ""
        )

    with pytest.raises(GCHostError, match="partial Docker CE"):
        inspect_container_runtime(policy, runner=partial)


def test_container_runtime_uses_ubuntu_packages_when_docker_ce_is_absent():
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))
    missing = lambda *_args, **_kwargs: __import__("subprocess").CompletedProcess(
        [], 1, "", ""
    )

    result = inspect_container_runtime(policy, runner=missing)

    assert result == {
        "provider": "ubuntu",
        "install_packages": policy["operational_packages"],
        "existing_packages": [],
    }


def _cache(tmp_path: Path, platform_id: str = "ubuntu-24.04-x86_64") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    policy = json.loads(POLICY.read_text())
    artifacts = []
    for index, role in enumerate(policy["offline_roles"]):
        path = tmp_path / f"artifact-{index}.tar"
        payload = (role + "\n").encode()
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo(f"{role}.artifact")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        artifacts.append(
            {
                "role": role,
                "path": path.name,
                "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
        )
    value = {
        "schema": "iii.gc-offline-cache/v1",
        "platform_id": platform_id,
        "created_at": "2026-08-27T00:00:00Z",
        "artifacts": artifacts,
    }
    value["cache_id"] = content_identity(value)
    (tmp_path / "gc-offline-cache.json").write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_offline_cache_reauthenticates_every_required_role(tmp_path):
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))

    cache = load_offline_cache(
        _cache(tmp_path),
        platform_id="ubuntu-24.04-x86_64",
        policy=policy,
        registry=ContractRegistry(SCHEMAS),
    )

    assert cache["cache_id"]
    assert {item["role"] for item in cache["artifacts"]} == set(policy["offline_roles"])
    assert all(Path(item["absolute_path"]).is_absolute() for item in cache["artifacts"])


def test_offline_cache_tamper_and_cross_platform_reuse_are_rejected(tmp_path):
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))
    root = _cache(tmp_path)
    (root / "artifact-0.tar").write_text("tampered\n")

    with pytest.raises(GCHostError, match="changed"):
        load_offline_cache(
            root,
            platform_id="ubuntu-24.04-x86_64",
            policy=policy,
            registry=ContractRegistry(SCHEMAS),
        )


def test_offline_cache_rejects_duplicate_and_unexpected_roles(tmp_path):
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))
    root = _cache(tmp_path)
    manifest_path = root / "gc-offline-cache.json"
    value = json.loads(manifest_path.read_text())
    value["artifacts"][-1]["role"] = value["artifacts"][0]["role"]
    value["cache_id"] = content_identity(
        {key: item for key, item in value.items() if key != "cache_id"}
    )
    manifest_path.write_text(json.dumps(value, sort_keys=True) + "\n")

    with pytest.raises(GCHostError, match="duplicated"):
        load_offline_cache(
            root,
            platform_id="ubuntu-24.04-x86_64",
            policy=policy,
            registry=ContractRegistry(SCHEMAS),
        )


def test_offline_cache_rejects_unsafe_archive_members_before_ansible(tmp_path):
    policy = load_policy(POLICY, ContractRegistry(SCHEMAS))
    root = _cache(tmp_path)
    manifest_path = root / "gc-offline-cache.json"
    value = json.loads(manifest_path.read_text())
    artifact = root / value["artifacts"][0]["path"]
    with tarfile.open(artifact, "w") as archive:
        payload = b"escape\n"
        info = tarfile.TarInfo("../escape")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    value["artifacts"][0]["sha256"] = (
        __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    )
    value["artifacts"][0]["size"] = artifact.stat().st_size
    value["cache_id"] = content_identity(
        {key: item for key, item in value.items() if key != "cache_id"}
    )
    manifest_path.write_text(json.dumps(value, sort_keys=True) + "\n")

    with pytest.raises(GCHostError, match="unsafe member"):
        load_offline_cache(
            root,
            platform_id="ubuntu-24.04-x86_64",
            policy=policy,
            registry=ContractRegistry(SCHEMAS),
        )

    other = _cache(tmp_path / "other", "ubuntu-22.04-x86_64")
    with pytest.raises(GCHostError, match="platform"):
        load_offline_cache(
            other,
            platform_id="ubuntu-24.04-x86_64",
            policy=policy,
            registry=ContractRegistry(SCHEMAS),
        )


def test_source_manifest_rejects_symlinks_and_ignores_generated_cache(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "owned.txt").write_text("owned\n")
    generated = tree / "__pycache__"
    generated.mkdir()
    (generated / "ignored.pyc").write_bytes(b"ignored")

    manifest = _tree_manifest(tree)
    assert [item["path"] for item in manifest["files"]] == ["owned.txt"]

    (tree / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(GCHostError, match="symbolic link"):
        _tree_manifest(tree)


def test_user_units_autostart_companions_but_never_browser_qgc_or_drone():
    templates = DEPLOYMENT / "ansible/roles/gc_operational/templates"
    target = (templates / "iii-gc.target.j2").read_text()
    combined = "\n".join(
        path.read_text() for path in sorted(templates.glob("*.service.j2"))
    )

    assert "PartOf=graphical-session.target" in target
    for unit in (
        "iii-gc-proxy.service",
        "iii-gc-frontend.service",
        "iii-gc-discovery.service",
        "iii-gc-mirror.service",
        "iii-gc-clock.service",
    ):
        assert unit in target
    assert "iii-gc-browser.service" not in target
    assert "qgroundcontrol" not in target.lower()
    qgc = (templates / "iii-qgc.service.j2").read_text()
    assert "WantedBy=" not in qgc
    assert "iii-gc.target" not in qgc
    assert "gc-applications/qgc/current/QGroundControl.AppImage" in qgc
    reconcile = (templates / "iii-gc-application-reconcile.service.j2").read_text()
    assert "Before=iii-gc.target" in reconcile
    assert "gc application reconcile" in reconcile
    assert "iii system stop" not in combined
    assert "Restart=on-failure" in combined


def test_operational_role_preserves_secret_files_and_never_installs_ros():
    role = (DEPLOYMENT / "ansible/roles/gc_operational/tasks/main.yml").read_text()
    policy = json.loads(POLICY.read_text())

    assert "force: false" in role
    assert "runtime-api.token" not in role
    assert all(
        not package.startswith("ros-") for package in policy["operational_packages"]
    )
    assert "mavsdk" not in policy["operational_packages"]
    assert "cyclonedds" not in policy["operational_packages"]


def test_development_role_keeps_authority_in_host_paths_and_strict_git_state():
    role = (DEPLOYMENT / "ansible/roles/gc_development/tasks/main.yml").read_text()

    assert "verify_submodule_lock.sh" in role
    assert "push.recurseSubmodules" in role
    assert "{{ iii_gc_home }}/.local/share/iii/controller" in role
    assert "{{ iii_gc_home }}/.cache/iii/ccache" in role
    assert "builder.definition_sha256" in role
    assert "io.iii-drone.builder-definition-sha256" in role
    assert "controller/environments/[a-f0-9]{64}" in role
    assert "pip check" not in role
    assert "- pip\n" in role
    assert "- install\n" not in role
    assert "/home/iii" not in role


def test_host_baseline_cannot_build_or_load_release_container_images():
    compose = (ROOT / "src/III-Drone-GC/docker-compose.prod.yml").read_text()
    application = (
        DEPLOYMENT / "ansible/roles/gc_application/tasks/main.yml"
    ).read_text()

    assert compose.count("io.iii-drone.application-id") == 2
    assert "docker compose" not in application
    assert "docker load" not in application
    assert "gc-container-images" not in application
    assert "GC runtime" in application
    assert "iii-drone-gc-proxy:workspace" not in application


def test_complete_replacement_plan_imports_records_before_creating_fresh_identity(
    tmp_path, monkeypatch
):
    source = tmp_path / "old-registry"
    record = source / "readiness/replacement.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        '{"schema":"iii.test-evidence/v1","value":"portable"}\n',
        encoding="utf-8",
    )
    archive = tmp_path / "portable-records.tar"
    record_registry.apply_archive_plan(
        source,
        record_registry.build_archive_plan(source, destination=archive),
    )
    home = tmp_path / "replacement-home"
    home.mkdir()
    account = {
        "name": "replacement-user",
        "uid": 1234,
        "gid": 1234,
        "home": str(home),
    }
    monkeypatch.setattr(gc_host, "_current_user", lambda *_args, **_kwargs: account)
    os_release = _os_release(tmp_path / "os-release", "24.04")
    ansible_playbook = Path("/tmp/iii-p3t4-ansible-venv/bin/ansible-playbook")
    if not ansible_playbook.is_file():
        pytest.skip("repository-managed test Ansible controller is unavailable")
    plan = build_plan(
        operation_id="replace-test-001",
        workspace=ROOT,
        policy_path=POLICY,
        schema_root=SCHEMAS,
        ansible_root=DEPLOYMENT / "ansible",
        ansible_playbook=ansible_playbook,
        replacement_archive=archive,
        os_release_path=os_release,
        architecture="x86_64",
    )
    assert plan["replacement"] is True
    assert plan["archive_import"]["conflicts"] == []
    assert plan["archive_import"]["missing_blob_ids"] == []

    imported_record = home / ".local/state/iii/registry/readiness/replacement.json"
    counters = {
        "ok": 1,
        "changed": 0,
        "failures": 0,
        "unreachable": 0,
        "skipped": 0,
        "rescued": 0,
        "ignored": 0,
    }

    def converge(_plan, *, check):
        assert (
            imported_record.is_file()
        ), "portable records must restore before convergence"
        identity = home / ".config/iii/identity/machine-id"
        public_key = home / ".config/iii/keys/ssh/id_ed25519.pub"
        identity.parent.mkdir(parents=True, exist_ok=True)
        public_key.parent.mkdir(parents=True, exist_ok=True)
        identity.write_text("fresh-machine-identity\n", encoding="utf-8")
        public_key.write_text("ssh-ed25519 fresh-replacement-key\n", encoding="utf-8")
        return {
            "schema": "iii.ansible-run-result/v1",
            "check_mode": check,
            "hosts": {"localhost": dict(counters)},
            "totals": dict(counters),
            "categories": {
                name: dict(counters)
                for name in ("operational", "application", "development")
            },
        }

    monkeypatch.setattr(gc_host, "_verify_plan", lambda *_args, **_kwargs: ({}, {}))
    monkeypatch.setattr(gc_host, "_sudo_ready", lambda: None)
    monkeypatch.setattr(gc_host, "_run_ansible", converge)

    report = apply_plan(plan, schema_root=SCHEMAS)

    assert report["state"] == "provisioned"
    assert report["archive_import"]["archive_id"]
    assert report["fresh_identity"]["private_material_exported"] is False
    assert report["fresh_identity"]["runtime_enrollment_required"] is True
    assert imported_record.read_text(encoding="utf-8").endswith("\n")


def test_replacement_refuses_existing_private_identity_or_runtime_credential(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    sensitive = home / ".config/iii/credentials/runtime-api.token"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text("never-copy-this\n", encoding="utf-8")
    account = {"name": "gc", "uid": 1234, "gid": 1234, "home": str(home)}

    with pytest.raises(GCHostError, match="fresh host"):
        gc_host._replacement_preflight(account)


def test_replacement_post_convergence_reauthenticates_import_and_fresh_keys(
    tmp_path,
):
    source = tmp_path / "portable"
    record = source / "readiness/replacement.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"schema":"iii.test/v1"}\n', encoding="utf-8")
    archive = tmp_path / "records.tar"
    record_registry.apply_archive_plan(
        source,
        record_registry.build_archive_plan(source, destination=archive),
    )
    home = tmp_path / "home"
    home.mkdir()
    destination = home / ".local/state/iii/registry"
    import_plan = record_registry.build_import_plan(destination, archive_path=archive)
    plan = {
        "user": {"home": str(home), "uid": os.getuid(), "gid": os.getgid()},
        "archive_import": import_plan,
    }

    with pytest.raises(gc_host.GCHostChangedError, match="import is incomplete"):
        gc_host._verify_replacement_converged(plan)

    record_registry.apply_import_plan(destination, import_plan)
    machine = home / ".config/iii/identity/machine-id"
    private = home / ".config/iii/keys/ssh/id_ed25519"
    public = home / ".config/iii/keys/ssh/id_ed25519.pub"
    for path, content, mode in (
        (machine, "fresh-machine\n", 0o600),
        (private, "fresh-private\n", 0o600),
        (public, "ssh-ed25519 fresh\n", 0o644),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)

    gc_host._verify_replacement_converged(plan)

    credential = home / ".config/iii/credentials/runtime-api.token"
    credential.parent.mkdir(parents=True)
    credential.write_text("must-not-be-restored\n", encoding="utf-8")
    with pytest.raises(gc_host.GCHostChangedError, match="enrollment"):
        gc_host._verify_replacement_converged(plan)
