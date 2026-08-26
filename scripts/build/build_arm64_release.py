#!/usr/bin/env python3
"""Build a validated immutable ARM64 install tree entirely offboard."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.build import (  # noqa: E402
    classify_package_cache, commit_package_cache_state, install_locked_wheels,
    installed_package_names, isolated_colcon_command, prepare_package_cache,
    load_build_policy, materialize_build_source, make_build_record,
    normalize_install_tree, package_cache_keys, package_graph, run_offboard_command,
    parse_compiler_cache_stats,
    select_build_packages, target_elf_closure_command, target_import_command, validate_release_tree,
    verify_installed_release_assets, write_release_wrapper,
)
from iii_deployment.contracts import (  # noqa: E402
    ContractError, ContractRegistry, canonical_json, content_identity,
)
from iii_deployment.source import (  # noqa: E402
    capture_source_snapshot, load_source_policy, validate_component_selection,
    verify_source_snapshot,
)
from iii_deployment.target import load_target_definition  # noqa: E402
from iii_deployment.mission_catalog import install_qualified_mission_catalog  # noqa: E402
from iii_deployment.wheels import load_wheel_lock  # noqa: E402


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--component", choices=("drone", "gc"), action="append", required=True)
    parser.add_argument(
        "--qualified-paired",
        action="store_true",
        help="Build only the drone payload while a separately validated GC artifact satisfies paired impact.",
    )
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--wheel-lock", type=Path, required=True)
    parser.add_argument("--image", default="iii-arm64-cross-builder:p1")
    parser.add_argument("--target-image", default="iii-arm64-target-runtime:p1")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    partial = args.output.parent / f".{args.output.name}.partial-{os.getpid()}"
    try:
        if args.output.exists() or partial.exists():
            raise ContractError("build output or private partial path already exists")
        snapshot = _json(args.snapshot)
        verify_source_snapshot(snapshot, registry)
        source_policy = load_source_policy(ROOT / "deployment/source-policy.json", registry)
        current_snapshot = capture_source_snapshot(ROOT, source_policy, registry)
        if current_snapshot["content_identity"] != snapshot["content_identity"]:
            raise ContractError("live source no longer matches the verified source snapshot")
        if args.qualified_paired:
            if sorted(set(args.component)) != ["drone"]:
                raise ContractError("qualified paired ARM64 build must request exactly the drone component")
            validate_component_selection(snapshot["impact"], ["drone", "gc"])
        else:
            validate_component_selection(snapshot["impact"], args.component)
        policy = load_build_policy(ROOT / "deployment/build-policy.json", registry)
        target = load_target_definition(ROOT / policy["target_definition"], registry)
        lock = load_wheel_lock(
            args.wheel_lock, ROOT / "deployment/python/requirements.in", target, registry
        )
        graph = package_graph(ROOT)
        packages = select_build_packages(graph, args.component, policy)
        impacted_packages = select_build_packages(
            graph, args.component, policy, snapshot["changed_paths"]
        )
        keys = package_cache_keys(
            graph, packages, snapshot["repositories"], target["definition_id"],
            snapshot["dependency_lock_sha256"],
        )
        package_paths = {metadata["path"] for metadata in graph.values()}
        cache_context = content_identity({
            "target": target["definition_id"],
            "dependency_lock": snapshot["dependency_lock_sha256"],
            "external_repositories": [
                {"path": repo["path"], "content_identity": repo["content_identity"]}
                for repo in snapshot["repositories"]
                if repo["path"] != "." and repo["path"] not in package_paths
            ],
            "builder_inputs": {
                path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                for path in (
                    "Dockerfile.cc", "cc_ws/arm64-toolchain.cmake",
                    "deployment/build-policy.json", "entrypoint_cc.sh",
                )
            },
        })
        cache, cache_state_path = classify_package_cache(args.cache, keys, cache_context)
        container_user = f"{os.getuid()}:{os.getgid()}"
        partial.mkdir(parents=True, mode=0o700)
        args.cache.mkdir(parents=True, exist_ok=True)
        prepare_package_cache(args.cache, cache)
        (args.cache / "ccache").mkdir(exist_ok=True)
        build_source = materialize_build_source(ROOT, partial / ".build-source")
        run_offboard_command([
            "docker", "buildx", "build", "--load", "--target", "cross-compiler",
            "--tag", args.image, "--file", "Dockerfile.cc", ".",
        ], cwd=ROOT)
        run_offboard_command([
            "docker", "buildx", "build", "--load", "--target", "target-runtime",
            "--tag", args.target_image, "--file", "Dockerfile.cc", ".",
        ], cwd=ROOT)
        run_offboard_command([
            "docker", "run", "--rm", "--network", "none",
            "--user", container_user, "-e", "HOME=/tmp",
            "-v", f"{args.cache.resolve()}:/cache", "--entrypoint", "ccache",
            args.image, "--zero-stats",
        ], cwd=ROOT)
        container_command = isolated_colcon_command(
            packages, build_base=Path("/cache/build"),
            install_base=Path(policy["activation_root"]) / policy["release_install"],
            log_base=Path("/cache/log"), skip_packages=set(graph) - set(packages),
        )
        run_offboard_command([
            "docker", "run", "--rm", "--network", "none",
            "--user", container_user, "-e", "HOME=/tmp",
            "-v", f"{build_source.resolve()}:/home/iii/ws", "-v", f"{args.cache.resolve()}:/cache",
            "-v", f"{partial.resolve()}:{policy['activation_root']}", args.image, *container_command,
        ], cwd=ROOT)
        cache["compiler"] = parse_compiler_cache_stats(run_offboard_command([
            "docker", "run", "--rm", "--network", "none",
            "--user", container_user, "-e", "HOME=/tmp",
            "-v", f"{args.cache.resolve()}:/cache", "--entrypoint", "ccache",
            args.image, "--print-stats",
        ], cwd=ROOT).stdout)
        cache["state_sha256"] = commit_package_cache_state(
            cache_state_path, keys, cache_context
        )
        mission_catalog = install_qualified_mission_catalog(partial / policy["release_install"])
        normalize_install_tree(partial / policy["release_install"])
        verify_installed_release_assets(build_source, partial, policy)
        installed_packages = installed_package_names(partial, policy)
        shutil.rmtree(build_source)
        python_lock_sha = install_locked_wheels(
            args.wheelhouse, partial / policy["python_site_packages"], lock
        )
        write_release_wrapper(partial, policy)
        run_offboard_command(
            target_import_command(
                args.target_image, partial.resolve(), [
                    *lock["imports"],
                    *(name for name in installed_packages if name.startswith("iii_drone_")),
                ],
                Path(policy["activation_root"]),
            ), cwd=ROOT
        )
        run_offboard_command(
            target_elf_closure_command(
                args.target_image, partial.resolve(), Path(policy["activation_root"]),
                policy["host_library_prefixes"],
                policy["host_elf_allowlist"],
            ), cwd=ROOT
        )
        validation = validate_release_tree(
            partial, policy, python_lock_sha256=python_lock_sha, target_elf_verified=True,
        )
        record = make_build_record(
            source_identity=snapshot["content_identity"], target_definition_id=target["definition_id"],
            policy=policy, components=args.component, packages=installed_packages,
            requested_packages=packages, impacted_packages=impacted_packages,
            cache_keys=keys, validation=validation, registry=registry,
            cache=cache,
        )
        (partial / "build-record.json").write_bytes(canonical_json(record) + b"\n")
        os.replace(partial, args.output)
        result = {"schema": "iii.arm64-build-result/v1", "outcome": "passed", "build_id": record["build_id"], "output": str(args.output), "packages": installed_packages, "requested_packages": packages, "impacted_packages": impacted_packages, "mission_catalog": mission_catalog}
        print(json.dumps(result, sort_keys=True) if args.json else f"PASS: {record['build_id']}")
        return 0
    except (ContractError, OSError) as exc:
        result = {"schema": "iii.arm64-build-result/v1", "outcome": "failed", "error": str(exc), "partial": str(partial) if partial.exists() else None}
        print(json.dumps(result, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
