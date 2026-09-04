from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from iii_deployment.build import (
    classify_package_cache,
    commit_package_cache_state,
    docker_build_resource_arguments,
    isolated_colcon_command,
    install_locked_wheels,
    installed_package_names,
    load_build_policy,
    materialize_build_source,
    make_build_record,
    normalize_install_tree,
    package_cache_keys,
    package_graph,
    parse_compiler_cache_stats,
    prepare_package_cache,
    run_offboard_command,
    run_bounded_target_check,
    select_build_packages,
    verify_installed_release_assets,
    install_deployment_release_resources,
    target_import_command,
    target_wheel_tag_compatible,
    target_elf_closure_command,
    validate_release_tree,
    write_release_wrapper,
)
from iii_deployment.contracts import ContractError, ContractRegistry


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
POLICY = load_build_policy(ROOT / "deployment/build-policy.json", REGISTRY)


@pytest.mark.parametrize("value", [
    "cp312-cp312-manylinux2014_aarch64",
    "cp310-abi3-manylinux2014_aarch64",
    "py3-none-any",
])
def test_target_wheel_accepts_cp312_and_compatible_stable_abi(value: str) -> None:
    from packaging.tags import parse_tag

    assert all(target_wheel_tag_compatible(tag) for tag in parse_tag(value))


@pytest.mark.parametrize("value", [
    "cp313-abi3-manylinux2014_aarch64",
    "cp312-cp312-manylinux2014_x86_64",
    "cp311-cp311-manylinux2014_aarch64",
])
def test_target_wheel_rejects_newer_or_wrong_native_abi(value: str) -> None:
    from packaging.tags import parse_tag

    assert not any(target_wheel_tag_compatible(tag) for tag in parse_tag(value))


def test_cross_toolchain_maps_absolute_and_colcon_relative_builder_paths() -> None:
    toolchain = (ROOT / "cc_ws/arm64-toolchain.cmake").read_text()
    for path in ("/home/iii/ws", "../../../home/iii/ws", "/opt/iii/sysroot", "/cache"):
        assert f"-ffile-prefix-map={path}=" in toolchain
        assert f"-fdebug-prefix-map={path}=" in toolchain
    assert "-fmacro-prefix-map=../../../home/iii/ws=" in toolchain
    assert "-fmacro-prefix-map=/opt/iii/sysroot=" in toolchain
    configuration_cmake = (ROOT / "src/III-Drone-Configuration/CMakeLists.txt").read_text()
    assert POLICY["target_python_extension_suffix"] in configuration_cmake


def test_build_source_omits_local_frontend_dependency_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    frontend = workspace / "src/III-Drone-GC/frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text('{"name":"fixture"}\n')
    node_modules = frontend / "node_modules"
    node_modules.mkdir()
    (node_modules / "external-tool").symlink_to("/outside/workspace/tool")

    result = materialize_build_source(workspace, tmp_path / "build-source")

    assert (result / "src/III-Drone-GC/frontend/package.json").is_file()
    assert not (result / "src/III-Drone-GC/frontend/node_modules").exists()


def test_cross_build_runs_target_generators_with_pinned_sysroot_emulator() -> None:
    toolchain = (ROOT / "cc_ws/arm64-toolchain.cmake").read_text()
    runner = (ROOT / "cc_ws/run-target-emulated.sh").read_text()
    dockerfile = (ROOT / "Dockerfile.cc").read_text()
    mission_cmake = (ROOT / "src/III-Drone-Mission/CMakeLists.txt").read_text()

    assert '"/usr/local/bin/iii-run-target-emulated"' in toolchain
    assert "/usr/bin/qemu-aarch64-static" in runner
    assert '-L "${sysroot}"' in runner
    assert '"${sysroot}/opt/ros/jazzy/lib"' in runner
    assert '"${sysroot}/usr/lib/aarch64-linux-gnu/blas"' in runner
    assert '"${sysroot}/usr/lib/aarch64-linux-gnu/lapack"' in runner
    assert '"${release_install}"/*/lib' in runner
    assert "COPY cc_ws/run-target-emulated.sh /usr/local/bin/iii-run-target-emulated" in dockerfile
    assert "COMMAND iii_behavior_node_contract_exporter" in mission_cmake
    assert 'COMMAND "$<TARGET_FILE:iii_behavior_node_contract_exporter>"' not in mission_cmake


def test_target_emulation_checks_are_bounded() -> None:
    builder = (ROOT / "scripts/build/build_arm64_release.py").read_text()
    assert builder.count("run_bounded_target_check(") == 2


def test_bounded_target_check_removes_container_after_timeout(monkeypatch, tmp_path) -> None:
    removed = []

    def fake_offboard(command, *, cwd):
        assert cwd == tmp_path
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text("a" * 64, encoding="utf-8")
        raise ContractError("timed out")

    def fake_run(command, **kwargs):
        removed.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("iii_deployment.build.run_offboard_command", fake_offboard)
    monkeypatch.setattr("iii_deployment.build.subprocess.run", fake_run)

    with pytest.raises(ContractError, match="timed out"):
        run_bounded_target_check(
            ["docker", "run", "--rm", "target:locked", "true"],
            cwd=tmp_path,
            timeout_sec=1,
        )

    assert removed[0][0] == ["docker", "rm", "--force", "a" * 64]


def _release(path: Path) -> Path:
    (path / "install/pkg/share/ament_index/resource_index/packages").mkdir(parents=True)
    (path / "install/setup.bash").write_text("# isolated setup\n")
    (path / "install/pkg/share/ament_index/resource_index/packages/pkg").write_text("")
    asset_marker = (
        path / "install/iwr6843aop_pub/share/ament_index/resource_index/packages/iwr6843aop_pub"
    )
    asset_marker.parent.mkdir(parents=True)
    asset_marker.write_text("")
    asset = path / "install/iwr6843aop_pub/share/iwr6843aop_pub/cfg_files/radar.cfg"
    asset.parent.mkdir(parents=True)
    asset.write_text("asset\n")
    (path / "python/cp312/site-packages/pkg").mkdir(parents=True)
    (path / "python/cp312/site-packages/pkg/__init__.py").write_text("VALUE = 1\n")
    write_release_wrapper(path, POLICY)
    return path


def test_package_graph_and_clean_build_select_all_component_packages() -> None:
    graph = package_graph(ROOT)
    selected = select_build_packages(graph, ["drone"], POLICY)
    assert selected == sorted(POLICY["components"]["drone"])
    command = isolated_colcon_command(
        selected, build_base=Path("/cache/build"), install_base=Path("/stage/install"),
        log_base=Path("/cache/log"), skip_packages=set(graph) - set(selected),
        parallel_workers=16,
    )
    assert "--symlink-install" not in command
    assert command[command.index("--install-base") + 1] == "/stage/install"
    assert "--packages-up-to" in command
    assert command[command.index("--packages-skip") + 1:]
    assert "iii_drone_simulation" in command
    assert command.index("iii_drone_simulation") > command.index("--packages-skip")
    assert command[command.index("--parallel-workers") + 1] == "16"


def test_cross_build_rejects_non_positive_parallel_worker_cap() -> None:
    with pytest.raises(ContractError, match="worker count must be positive"):
        isolated_colcon_command(
            ["iii_drone_core"],
            build_base=Path("/cache/build"),
            install_base=Path("/stage/install"),
            log_base=Path("/cache/log"),
            parallel_workers=0,
        )


def test_cross_build_worker_cap_is_enforced_by_docker_and_build_tools() -> None:
    assert docker_build_resource_arguments(16) == [
        "--cpus", "16",
        "-e", "CMAKE_BUILD_PARALLEL_LEVEL=16",
        "-e", "MAKEFLAGS=-j16",
    ]
    assert docker_build_resource_arguments(None) == []
    with pytest.raises(ContractError, match="worker count must be positive"):
        docker_build_resource_arguments(0)


def test_no_change_rebuild_has_stable_cache_keys() -> None:
    graph = package_graph(ROOT)
    repositories = [
        {"path": metadata["path"], "content_identity": f"{index:064x}"}
        for index, metadata in enumerate(graph.values(), start=1)
    ]
    packages = sorted(POLICY["components"]["drone"])
    first = package_cache_keys(graph, packages, repositories, "a" * 64)
    second = package_cache_keys(graph, packages, repositories, "a" * 64)
    assert first == second
    changed = deepcopy(repositories)
    changed[0]["content_identity"] = "f" * 64
    assert package_cache_keys(graph, packages, changed, "a" * 64) != first


def test_cache_evidence_requires_matching_key_and_build_tree(tmp_path: Path) -> None:
    keys = {"alpha": "a" * 64, "beta": "b" * 64}
    cache = tmp_path / "cache"
    (cache / "build/alpha").mkdir(parents=True)
    state = cache / "iii-package-cache-keys.json"
    identity = commit_package_cache_state(state, keys, "c" * 64)
    evidence, returned_state = classify_package_cache(cache, keys, "c" * 64)
    assert returned_state == state
    assert evidence == {
        "context": "c" * 64, "reset": False,
        "hits": ["alpha"], "misses": ["beta"],
    }
    assert len(identity) == 64
    (cache / "build/beta").mkdir()
    prepare_package_cache(cache, evidence)
    assert (cache / "build/alpha").is_dir()
    assert not (cache / "build/beta").exists()
    reset, _ = classify_package_cache(cache, keys, "d" * 64)
    assert reset["reset"] is True
    prepare_package_cache(cache, reset)
    assert not (cache / "build").exists()
    state.write_text("not-json")
    with pytest.raises(ContractError, match="cache state"):
        classify_package_cache(cache, keys, "c" * 64)


def test_compiler_cache_statistics_are_machine_parsed() -> None:
    stats = parse_compiler_cache_stats(
        "direct_cache_hit\t3\npreprocessed_cache_hit\t2\ncache_miss\t7\n"
        "files_in_cache\t11\ncache_size_kibibyte\t42\n"
    )
    assert stats == {
        "direct_hits": 3, "preprocessed_hits": 2, "misses": 7,
        "files": 11, "size_kibibytes": 42,
    }
    with pytest.raises(ContractError, match="malformed"):
        parse_compiler_cache_stats("cache_miss nope")


def test_single_package_and_interface_change_rebuild_selection() -> None:
    graph = package_graph(ROOT)
    single = select_build_packages(
        graph, ["drone"], POLICY, ["src/III-Drone-Mission/src/mission.cpp"]
    )
    assert "iii_drone_mission" in single
    assert "iii_drone_interfaces" not in single
    interface = select_build_packages(
        graph, ["drone"], POLICY, ["src/III-Drone-Interfaces/msg/Test.msg"]
    )
    assert interface == sorted(POLICY["components"]["drone"])


def test_valid_release_is_isolated_relocatable_and_recorded(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    validation = validate_release_tree(release, POLICY, python_lock_sha256="b" * 64)
    record = make_build_record(
        source_identity="a" * 64, target_definition_id="c" * 64, policy=POLICY,
        components=["drone"], packages=["pkg"], cache_keys={"pkg": "d" * 64},
        validation=validation, registry=REGISTRY,
    )
    assert record["complete"] is True
    assert validation["elf"]["closure_verified"] is True
    assert validation["assets"]["files"] == 1
    wrapper = (release / POLICY["release_wrapper"]).read_text()
    assert wrapper.index('source "${root}/install/setup.bash"') < wrapper.index("set -u")


def test_release_rejects_host_named_target_python_extension(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    extension = (
        release
        / "install/pkg/lib/python3.12/site-packages/pkg/_native.cpython-312-x86_64-linux-gnu.so"
    )
    extension.parent.mkdir(parents=True, exist_ok=True)
    extension.write_bytes(b"not needed to prove the filename contract")

    with pytest.raises(ContractError, match="wrong target ABI"):
        validate_release_tree(release, POLICY, python_lock_sha256="b" * 64)


def test_escaping_symlink_builder_path_and_development_tree_fail(tmp_path: Path) -> None:
    release = _release(tmp_path / "release")
    (release / "install/pkg/bad-link").symlink_to("/home/iii/ws/source")
    with pytest.raises(ContractError, match="symlink escapes"):
        validate_release_tree(release, POLICY, python_lock_sha256="b" * 64)
    (release / "install/pkg/bad-link").unlink()
    (release / "install/pkg/config.txt").write_text("/home/iii/ws/src")
    with pytest.raises(ContractError, match="builder path contamination"):
        validate_release_tree(release, POLICY, python_lock_sha256="b" * 64)
    (release / "install/pkg/config.txt").unlink()
    (release / "src").mkdir()
    with pytest.raises(ContractError, match="forbidden development tree"):
        validate_release_tree(release, POLICY, python_lock_sha256="b" * 64)


def test_deliberate_build_failure_cannot_create_complete_record(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="deliberate"):
        run_offboard_command(["bash", "-c", "echo deliberate >&2; exit 9"], cwd=tmp_path)
    with pytest.raises(ContractError, match="offboard-local"):
        run_offboard_command(["ssh", "iii@aircraft", "colcon build"], cwd=tmp_path)
    assert not (tmp_path / "build-record.json").exists()


def test_policy_forbids_target_package_managers_and_nonlocal_transport() -> None:
    assert POLICY["normal_build_transports"] == ["local-docker"]
    assert POLICY["target_package_manager_allowed"] is False
    assert POLICY["layout"] == "isolated-colcon"


def test_hashed_cp312_wheels_install_offline_and_target_import_has_no_network(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    filename = "demo-1.0-py3-none-any.whl"
    wheel = wheelhouse / filename
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo/__init__.py", "VALUE = 1\n")
        executable = zipfile.ZipInfo("demo/bin/helper")
        executable.create_system = 3
        executable.external_attr = 0o100755 << 16
        archive.writestr(executable, "#!/bin/sh\n")
        archive.writestr("demo/assets.data/resource.txt", "ordinary dotted package data\n")
        archive.writestr("demo-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
    lock = {
        "schema": "iii.python-wheel-lock/v1", "python_abi": "cp312",
        "platform": "manylinux_2_39_aarch64", "requirements_sha256": "a" * 64,
        "resolver": {
            "reference": "docker.io/library/python:3.12.3-slim-bookworm",
            "index_digest": "sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63",
            "platform": "linux/amd64",
            "platform_digest": "sha256:fd3817f3a855f6c2ada16ac9468e5ee93e361005bd226fd5a5ee1a504e038c84",
            "pip_version": "24.0",
        },
        "wheels": [{"name": "demo", "version": "1.0", "filename": filename,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "tags": ["py3-none-any"], "requires": []}],
        "imports": ["demo"],
    }
    REGISTRY.validate("python-wheel-lock", lock)
    site = tmp_path / "release/python/cp312/site-packages"
    assert len(install_locked_wheels(wheelhouse, site, lock)) == 64
    assert (site / "demo/__init__.py").is_file()
    assert (site / "demo/bin/helper").stat().st_mode & 0o111
    assert (site / "demo/assets.data/resource.txt").is_file()
    command = target_import_command("target:locked", tmp_path / "release", ["demo"])
    assert "none" in command
    assert any(value.endswith(":/opt/iii/current:ro") for value in command)
    assert "/opt/iii/current/bin/iii-release-env" in command
    elf_command = target_elf_closure_command("target:locked", tmp_path / "release")
    assert "none" in elf_command
    assert "/opt/iii/current/bin/iii-release-env" in elf_command
    assert "resolves forbidden library" in elf_command[-1]
    assert "allowed_sonames" in elf_command[-1]
    bad = deepcopy(lock)
    bad["wheels"][0]["sha256"] = "0" * 64
    with pytest.raises(ContractError, match="hash mismatch"):
        install_locked_wheels(wheelhouse, tmp_path / "bad", bad)


def test_assets_install_inventory_and_sysroot_prefix_normalization(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src/iwr6843aop-ROS2-pkg/cfg_files").mkdir(parents=True)
    (source / "src/iwr6843aop-ROS2-pkg/cfg_files/radar.cfg").write_text("profile\n")
    release = tmp_path / "release"
    marker = (
        release
        / "install/iwr6843aop_pub/share/ament_index/resource_index/packages/iwr6843aop_pub"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    installed = (
        release / "install/iwr6843aop_pub/share/iwr6843aop_pub/cfg_files/radar.cfg"
    )
    installed.parent.mkdir(parents=True)
    installed.write_text("profile\n")
    metadata = verify_installed_release_assets(source, release, POLICY)
    assert metadata["files"] == 1
    installed.write_text("different\n")
    with pytest.raises(ContractError, match="differs from source"):
        verify_installed_release_assets(source, release, POLICY)
    package_marker = release / "install/pkg/share/ament_index/resource_index/packages/pkg"
    package_marker.parent.mkdir(parents=True)
    package_marker.write_text("")
    setup = release / "install/setup.bash"
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text("/opt/iii/sysroot/opt/ros/jazzy/setup.bash\n")
    bytecode = release / "install/pkg/lib/python3.12/site-packages/pkg/__pycache__/module.pyc"
    bytecode.parent.mkdir(parents=True)
    bytecode.write_bytes(b"/home/iii/ws/module.py")
    normalize_install_tree(release / "install")
    assert setup.read_text() == "/opt/ros/jazzy/setup.bash\n"
    assert not bytecode.exists()
    assert installed_package_names(release, POLICY) == ["iwr6843aop_pub", "pkg"]


def test_deployment_px4_contract_is_installed_for_receiver_audit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "deployment/px4"
    source.mkdir(parents=True)
    (source / "firmware.json").write_text("{}\n")
    (source / "real.json").write_text("{}\n")
    install = tmp_path / "release/install"

    metadata = install_deployment_release_resources(workspace, install)

    destination = install / "share/iii-deployment/px4"
    assert metadata["files"] == 2
    assert (destination / "firmware.json").read_bytes() == b"{}\n"
    assert (destination / "real.json").read_bytes() == b"{}\n"
    with pytest.raises(ContractError, match="destination already exists"):
        install_deployment_release_resources(workspace, install)
