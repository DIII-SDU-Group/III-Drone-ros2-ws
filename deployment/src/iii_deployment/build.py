"""Offboard cached build planning and immutable install-tree validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping
import xml.etree.ElementTree as ET
import zipfile

from packaging.tags import parse_tag

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .wheels import verify_wheelhouse


# Local dependency and frontend build trees are not governed source.  In
# particular, package-manager command shims are commonly symlinks outside the
# checkout; copying them into a release build both contaminates the Docker
# context and correctly trips the immutable-input escape check below.
BUILD_SOURCE_EXCLUDES = {
    ".git", "build", "install", "log", "runtime", "runtime_logs",
    "__pycache__", ".pytest_cache", "node_modules", "dist", ".vite", ".turbo",
}


def target_wheel_tag_compatible(tag: Any) -> bool:
    """Return whether a wheel tag can run on the CPython 3.12 ARM64 target."""
    platform_compatible = (
        tag.platform == "any"
        or tag.platform == "linux_aarch64"
        or (tag.platform.startswith("manylinux") and tag.platform.endswith("_aarch64"))
    )
    if not platform_compatible:
        return False
    if tag.interpreter in {"cp312", "py3"} and tag.abi in {"cp312", "abi3", "none"}:
        return True
    match = re.fullmatch(r"cp(\d)(\d+)", tag.interpreter)
    return bool(
        tag.abi == "abi3"
        and match
        and (int(match.group(1)), int(match.group(2))) <= (3, 12)
    )


def materialize_build_source(workspace: Path, destination: Path) -> Path:
    """Create a disposable writable build input; never mutate the live checkout."""
    if destination.exists():
        raise ContractError("private build-source destination already exists")
    source = workspace / "src"
    if not source.is_dir():
        raise ContractError("workspace source directory is missing")
    shutil.copytree(
        source, destination / "src", symlinks=True,
        ignore=lambda _directory, names: sorted(set(names).intersection(BUILD_SOURCE_EXCLUDES)),
    )
    for path in (destination / "src").rglob("*"):
        if not path.is_symlink():
            continue
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(destination):
            raise ContractError(f"build-source symlink escapes materialization: {path}")
    return destination


def load_build_policy(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load build policy: {exc}") from exc
    registry.validate("build-policy", value)
    if value["target_package_manager_allowed"] or value["normal_build_transports"] != ["local-docker"]:
        raise ContractError("build policy permits target mutation or a non-local transport")
    if value["host_elf_allowlist"] != sorted(set(value["host_elf_allowlist"])):
        raise ContractError("host ELF allowlist must be unique and sorted")
    return value


def package_graph(root: Path) -> dict[str, dict[str, Any]]:
    graph: dict[str, dict[str, Any]] = {}
    for manifest in sorted(root.glob("src/III-Drone-*/package.xml")):
        try:
            xml = ET.parse(manifest).getroot()
            name = (xml.findtext("name") or "").strip()
        except (ET.ParseError, OSError) as exc:
            raise ContractError(f"cannot parse package manifest {manifest}: {exc}") from exc
        if not name or name in graph:
            raise ContractError(f"ambiguous package name in {manifest}")
        dependencies = sorted({
            (element.text or "").strip()
            for tag in ("depend", "build_depend", "build_export_depend", "exec_depend")
            for element in xml.findall(tag)
            if (element.text or "").strip()
        })
        graph[name] = {"path": str(manifest.parent.relative_to(root)), "dependencies": dependencies}
    # A small number of upstream CMake projects are valid colcon packages but
    # intentionally do not carry ROS package.xml metadata. Include only their
    # explicit colcon.pkg descriptor so release policy can name such a runtime
    # dependency without teaching the builder package-specific paths.
    for manifest in sorted(root.glob("src/*/colcon.pkg")):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot parse colcon package manifest {manifest}: {exc}") from exc
        name = value.get("name")
        dependencies = value.get("dependencies", [])
        if (
            not isinstance(name, str)
            or not name
            or name in graph
            or not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
        ):
            raise ContractError(f"ambiguous colcon package manifest {manifest}")
        graph[name] = {
            "path": str(manifest.parent.relative_to(root)),
            "dependencies": sorted(set(dependencies)),
        }
    if not graph:
        raise ContractError("no editable III packages were discovered")
    return graph


def select_build_packages(
    graph: Mapping[str, Mapping[str, Any]], components: Iterable[str],
    policy: Mapping[str, Any], changed_paths: Iterable[str] = (),
) -> list[str]:
    requested = sorted(set(components))
    unknown = sorted(set(requested) - set(policy["components"]))
    if unknown:
        raise ContractError("unknown build component: " + ", ".join(unknown))
    selected = {package for component in requested for package in policy["components"][component]}
    missing = sorted(selected - set(graph))
    if missing:
        raise ContractError("build policy references missing III package: " + ", ".join(missing))
    changed_packages = {
        name for name, metadata in graph.items()
        if any(path == metadata["path"] or path.startswith(metadata["path"] + "/") for path in changed_paths)
    }
    if changed_packages:
        downstream = set(changed_packages)
        progress = True
        while progress:
            progress = False
            for name, metadata in graph.items():
                if name not in downstream and downstream.intersection(metadata["dependencies"]):
                    downstream.add(name)
                    progress = True
        selected &= downstream
        # Interface changes always rebuild every selected component package.
        if changed_packages.intersection(policy["interface_packages"]):
            selected = {package for component in requested for package in policy["components"][component]}
    return sorted(selected)


def package_cache_keys(
    graph: Mapping[str, Mapping[str, Any]], packages: Iterable[str],
    source_repositories: Iterable[Mapping[str, Any]], target_definition_id: str,
    dependency_lock_sha256: str = "0" * 64,
) -> dict[str, str]:
    identities = {repo["path"]: repo["content_identity"] for repo in source_repositories}
    keys: dict[str, str] = {}
    selected = set(packages)
    visiting: set[str] = set()

    def resolve(package: str) -> str:
        if package in keys:
            return keys[package]
        if package in visiting:
            raise ContractError(f"cyclic III package dependency at {package}")
        visiting.add(package)
        metadata = graph[package]
        dependencies = {
            dependency: resolve(dependency) if dependency in selected else "host-or-lock"
            for dependency in metadata["dependencies"]
        }
        keys[package] = content_identity({
            "target": target_definition_id,
            "dependency_lock": dependency_lock_sha256,
            "source": identities.get(metadata["path"], "workspace"),
            "dependencies": dependencies,
        })
        visiting.remove(package)
        return keys[package]

    for package in sorted(selected):
        resolve(package)
    return keys


def isolated_colcon_command(
    packages: Iterable[str], *, build_base: Path, install_base: Path, log_base: Path,
    skip_packages: Iterable[str] = (),
    toolchain: Path = Path("/opt/iii/arm64-toolchain.cmake"),
    parallel_workers: int | None = None,
) -> list[str]:
    selected = sorted(set(packages))
    skipped = sorted(set(skip_packages))
    if not selected:
        raise ContractError("refusing an empty build")
    if set(selected).intersection(skipped):
        raise ContractError("selected and skipped build packages overlap")
    command = [
        "colcon", "--log-base", str(log_base), "build", "--base-paths", "src",
        "--build-base", str(build_base), "--install-base", str(install_base),
        "--packages-up-to", *selected,
    ]
    if parallel_workers is not None:
        if parallel_workers < 1:
            raise ContractError("parallel build worker count must be positive")
        command.extend(["--parallel-workers", str(parallel_workers)])
    if skipped:
        command.extend(["--packages-skip", *skipped])
    command.extend([
        "--packages-skip-regex", "example_.*", "--cmake-args",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TESTING=OFF",
        "-DBTCPP_GROOT_INTERFACE=OFF", "--no-warn-unused-cli",
    ])
    return command


def _tree_identity(root: Path) -> str:
    entries: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(canonical_json(entries)).hexdigest()


def _installed_release_asset_metadata(
    release_root: Path, policy: Mapping[str, Any]
) -> dict[str, Any]:
    inventory: list[dict[str, Any]] = []
    for item in policy["release_assets"]:
        package = item["package"]
        package_prefix = release_root / policy["release_install"] / package
        marker = (
            package_prefix / "share/ament_index/resource_index/packages" / package
        )
        share = package_prefix / "share" / package
        destination = (share / item["relative"]).resolve()
        if not marker.is_file() or not destination.is_dir() or not destination.is_relative_to(share.resolve()):
            raise ContractError(
                f"ament-installed release asset is missing or invalid: {package}/{item['relative']}"
            )
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise ContractError(f"ament-installed release asset contains a symlink: {path}")
        files = sum(path.is_file() for path in destination.rglob("*"))
        if not files:
            raise ContractError(f"ament-installed release asset is empty: {package}/{item['relative']}")
        inventory.append({
            "package": package,
            "relative": item["relative"],
            "sha256": _tree_identity(destination),
            "files": files,
        })
    return {
        "sha256": hashlib.sha256(canonical_json(inventory)).hexdigest(),
        "files": sum(item["files"] for item in inventory),
    }


def verify_installed_release_assets(
    build_source: Path, release_root: Path, policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Require package-owned ament assets to match their governed source bytes."""
    metadata = _installed_release_asset_metadata(release_root, policy)
    for item in policy["release_assets"]:
        source = (build_source / item["source"]).resolve()
        share = (
            release_root / policy["release_install"] / item["package"]
            / "share" / item["package"]
        )
        installed = (share / item["relative"]).resolve()
        if not source.is_dir() or not source.is_relative_to(build_source.resolve()):
            raise ContractError(f"release asset source is missing or escapes build input: {item['source']}")
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ContractError(f"release asset source contains a symlink: {path}")
        if _tree_identity(source) != _tree_identity(installed):
            raise ContractError(
                f"ament-installed release asset differs from source: {item['package']}/{item['relative']}"
            )
    return metadata


def install_deployment_release_resources(
    workspace: Path, install_root: Path
) -> dict[str, Any]:
    """Install the deployment-owned PX4 contract used by receiver audits."""
    source = (workspace / "deployment/px4").resolve()
    destination = install_root / "share/iii-deployment/px4"
    if not source.is_dir() or not source.is_relative_to(workspace.resolve()):
        raise ContractError("deployment PX4 release resources are missing or escape the workspace")
    if destination.exists() or destination.is_symlink():
        raise ContractError("deployment PX4 release resource destination already exists")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ContractError(f"deployment PX4 release resource contains a symlink: {path}")
    shutil.copytree(source, destination)
    if _tree_identity(source) != _tree_identity(destination):
        raise ContractError("installed deployment PX4 release resources differ from source")
    files = sum(path.is_file() for path in destination.rglob("*"))
    if not files:
        raise ContractError("deployment PX4 release resources are empty")
    return {"sha256": _tree_identity(destination), "files": files}


def normalize_install_tree(install: Path) -> None:
    """Normalize build-only prefixes and remove path-bearing Python bytecode."""
    if not install.is_dir():
        raise ContractError("cannot normalize a missing install tree")
    source = b"/opt/iii/sysroot/opt/ros/jazzy"
    destination = b"/opt/ros/jazzy"
    for path in install.rglob("*"):
        if path.is_file() and path.suffix == ".pyc":
            path.unlink()
            continue
        if not path.is_file() or path.is_symlink() or _is_elf(path):
            continue
        content = path.read_bytes()
        if source in content:
            path.write_bytes(content.replace(source, destination))
    for path in sorted(install.rglob("__pycache__"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def classify_package_cache(
    cache_root: Path, cache_keys: Mapping[str, str], context: str
) -> tuple[dict[str, Any], Path]:
    """Classify content-addressed package cache evidence before a build."""
    state_path = cache_root / "iii-package-cache-keys.json"
    previous: dict[str, str] = {}
    previous_context: str | None = None
    if state_path.exists():
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot read package cache state: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != "iii.package-cache-state/v1"
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("context", "")))
            or not isinstance(value.get("keys"), dict)
            or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{64}", identity)
            for name, identity in value.get("keys", {}).items()
            )
        ):
            raise ContractError("package cache state is malformed")
        previous = value["keys"]
        previous_context = value["context"]
    build_root = cache_root / "build"
    reset = (build_root.exists() and previous_context is None) or (
        previous_context is not None and previous_context != context
    )
    hits = sorted(
        name for name, identity in cache_keys.items()
        if not reset and previous.get(name) == identity and (build_root / name).is_dir()
    )
    return {
        "context": context, "reset": reset,
        "hits": hits, "misses": sorted(set(cache_keys) - set(hits)),
    }, state_path


def prepare_package_cache(cache_root: Path, evidence: Mapping[str, Any]) -> None:
    """Remove only invalidated disposable build entries; preserve ccache."""
    root = cache_root.resolve()
    if root == Path("/") or root == Path.home() or len(root.parts) < 3:
        raise ContractError("unsafe package cache root")
    build_root = root / "build"
    if build_root.is_symlink():
        raise ContractError("package build cache cannot be a symlink")
    if evidence["reset"] and build_root.exists():
        shutil.rmtree(build_root)
        return
    for package in evidence["misses"]:
        path = build_root / package
        if path.is_symlink():
            raise ContractError(f"package build cache entry is a symlink: {package}")
        if path.is_dir():
            shutil.rmtree(path)


def commit_package_cache_state(
    state_path: Path, cache_keys: Mapping[str, str], context: str
) -> str:
    value = {
        "schema": "iii.package-cache-state/v1", "context": context,
        "keys": dict(sorted(cache_keys.items())),
    }
    payload = canonical_json(value) + b"\n"
    temporary = state_path.with_name(f".{state_path.name}.partial-{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, state_path)
    except OSError as exc:
        raise ContractError(f"cannot commit package cache state: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def parse_compiler_cache_stats(output: str) -> dict[str, int]:
    counters: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[1].isdigit():
            raise ContractError("compiler cache returned malformed statistics")
        counters[fields[0]] = int(fields[1])
    required = {
        "direct_cache_hit", "preprocessed_cache_hit", "cache_miss",
        "files_in_cache", "cache_size_kibibyte",
    }
    if not required.issubset(counters):
        raise ContractError("compiler cache statistics are incomplete")
    return {
        "direct_hits": counters["direct_cache_hit"],
        "preprocessed_hits": counters["preprocessed_cache_hit"],
        "misses": counters["cache_miss"],
        "files": counters["files_in_cache"],
        "size_kibibytes": counters["cache_size_kibibyte"],
    }


def installed_package_names(release_root: Path, policy: Mapping[str, Any]) -> list[str]:
    install = release_root / policy["release_install"]
    names = sorted({
        marker.name
        for marker in install.glob("*/share/ament_index/resource_index/packages/*")
        if marker.is_file()
    })
    if not names:
        raise ContractError("isolated install contains no ament package markers")
    return names


def _is_elf(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError as exc:
        raise ContractError(f"cannot inspect release file {path}: {exc}") from exc


def _file_contains(path: Path, fragment: bytes) -> bool:
    overlap = max(len(fragment) - 1, 0)
    previous = b""
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                value = previous + chunk
                if fragment in value:
                    return True
                previous = value[-overlap:] if overlap else b""
    except OSError as exc:
        raise ContractError(f"cannot scan release file {path}: {exc}") from exc
    return False


def _elf_dynamic(path: Path) -> tuple[set[str], list[str]]:
    process = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True, check=False)
    if process.returncode:
        raise ContractError(f"cannot read ELF dynamic section: {path}")
    needed = set(re.findall(r"\(NEEDED\).*?\[(.+?)\]", process.stdout))
    runpaths = re.findall(r"\((?:RPATH|RUNPATH)\).*?\[(.+?)\]", process.stdout)
    return needed, [item for value in runpaths for item in value.split(":")]


def validate_release_tree(
    release_root: Path, policy: Mapping[str, Any], *, sysroot: Path | None = None,
    python_lock_sha256: str, target_elf_verified: bool = False,
) -> dict[str, Any]:
    release_root = release_root.resolve()
    install = release_root / policy["release_install"]
    if not install.is_dir() or not (install / "setup.bash").is_file():
        raise ContractError("isolated install tree is incomplete (install/setup.bash missing)")
    forbidden = [name for name in policy["forbidden_output_directories"] if (release_root / name).exists()]
    if forbidden:
        raise ContractError("release contains forbidden development tree: " + ", ".join(forbidden))
    for path in release_root.rglob("*"):
        if path.is_symlink():
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(release_root):
                raise ContractError(f"release symlink escapes immutable root: {path}")
        if path.is_file():
            for fragment in policy["forbidden_path_fragments"]:
                if _file_contains(path, fragment.encode()):
                    raise ContractError(f"builder path contamination in {path}: {fragment}")
    bundled = {path.name for path in release_root.rglob("*.so*") if path.is_file()}
    target_extension_suffix = policy["target_python_extension_suffix"]
    wrong_python_extensions = sorted(
        str(path.relative_to(release_root))
        for path in release_root.rglob("*.cpython-*.so")
        if not path.name.endswith(target_extension_suffix)
    )
    if wrong_python_extensions:
        raise ContractError(
            "release contains Python extensions for the wrong target ABI: "
            + ", ".join(wrong_python_extensions)
        )
    elf_count = 0
    for path in release_root.rglob("*"):
        if not _is_elf(path):
            continue
        elf_count += 1
        needed, runpaths = _elf_dynamic(path)
        bad_runpaths = [value for value in runpaths if value.startswith("/") and not value.startswith("/opt/ros/jazzy")]
        if bad_runpaths:
            raise ContractError(f"absolute ELF RUNPATH in {path}: {', '.join(bad_runpaths)}")
        unresolved = needed - bundled - set(policy["host_elf_allowlist"])
        if unresolved and sysroot is None and not target_elf_verified:
            raise ContractError(f"ELF closure needs a target sysroot: {path}: {', '.join(sorted(unresolved))}")
        for library in sorted(unresolved):
            if target_elf_verified:
                continue
            matches = list(sysroot.glob(f"**/{library}")) if sysroot else []
            allowed = any(
                str(match.relative_to(sysroot)).startswith(prefix.lstrip("/") + "/")
                for match in matches for prefix in policy["host_library_prefixes"]
            )
            if not allowed:
                raise ContractError(f"unresolved or unapproved ELF dependency {library} required by {path}")
    wrapper = release_root / policy["release_wrapper"]
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise ContractError("release-owned executable environment wrapper is missing")
    python_root = release_root / policy["python_site_packages"]
    if not python_root.is_dir():
        raise ContractError("release-local cp312 site-packages tree is missing")
    assets = _installed_release_asset_metadata(release_root, policy)
    return {
        "install_sha256": _tree_identity(install),
        "assets": assets,
        "python": {"abi": "cp312", "lock_sha256": python_lock_sha256, "imports_verified": True},
        "elf": {"scanned": elf_count, "closure_verified": True},
    }


def write_release_wrapper(release_root: Path, policy: Mapping[str, Any]) -> Path:
    path = release_root / policy["release_wrapper"]
    path.parent.mkdir(parents=True, exist_ok=True)
    value = """#!/usr/bin/env bash
set -eo pipefail
readonly root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
export PYTHONPATH="${root}/python/cp312/site-packages${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
export LD_LIBRARY_PATH="${root}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "${root}/install/setup.bash"
set -u
exec "$@"
"""
    path.write_text(value, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_offboard_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    if not command or command[0] in {"ssh", "scp", "rsync"} or any(
        token.startswith(("ssh://", "iii@", "root@")) for token in command
    ):
        raise ContractError("build command is not an offboard-local action")
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        output.append(line)
        print(line, end="", file=sys.stderr, flush=True)
    returncode = process.wait()
    value = "".join(output)
    completed = subprocess.CompletedProcess(command, returncode, stdout=value, stderr="")
    if returncode:
        detail = "".join(output[-200:]).strip()
        raise ContractError(detail or "offboard build failed")
    return completed


def docker_build_resource_arguments(parallel_workers: int | None) -> list[str]:
    """Return a hard Docker CPU quota plus cooperative build-tool limits."""
    if parallel_workers is None:
        return []
    if parallel_workers < 1:
        raise ContractError("parallel worker count must be positive")
    workers = str(parallel_workers)
    return [
        "--cpus", workers,
        "-e", f"CMAKE_BUILD_PARALLEL_LEVEL={workers}",
        "-e", f"MAKEFLAGS=-j{workers}",
    ]


def run_bounded_target_check(
    command: list[str], *, cwd: Path, timeout_sec: int = 300
) -> subprocess.CompletedProcess[str]:
    """Run an ephemeral target container and remove it even after CLI timeout."""
    if command[:2] != ["docker", "run"]:
        raise ContractError("bounded target check must be a docker run command")
    if timeout_sec <= 0:
        raise ContractError("bounded target check timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="iii-target-check-") as directory:
        cidfile = Path(directory) / "container.cid"
        docker_command = [*command[:2], "--cidfile", str(cidfile), *command[2:]]
        try:
            return run_offboard_command(
                [
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=10s",
                    f"{timeout_sec}s",
                    *docker_command,
                ],
                cwd=cwd,
            )
        finally:
            if cidfile.is_file():
                container_id = cidfile.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-f]{12,64}", container_id):
                    subprocess.run(
                        ["docker", "rm", "--force", container_id],
                        cwd=cwd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )


def make_build_record(
    *, source_identity: str, target_definition_id: str, policy: Mapping[str, Any],
    components: Iterable[str], packages: Iterable[str], cache_keys: Mapping[str, str],
    validation: Mapping[str, Any], registry: ContractRegistry,
    requested_packages: Iterable[str] | None = None,
    impacted_packages: Iterable[str] = (),
    cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    installed = sorted(set(packages))
    requested = sorted(set(requested_packages if requested_packages is not None else installed))
    cache_value = dict(cache or {
        "context": "0" * 64, "reset": False, "hits": [],
        "misses": sorted(cache_keys), "state_sha256": "0" * 64,
        "compiler": {
            "direct_hits": 0, "preprocessed_hits": 0, "misses": 0,
            "files": 0, "size_kibibytes": 0,
        },
    })
    if (
        set(cache_value.get("hits", [])).intersection(cache_value.get("misses", []))
        or set(cache_value.get("hits", [])).union(cache_value.get("misses", [])) != set(cache_keys)
    ):
        raise ContractError("cache hit/miss evidence does not partition package cache keys")
    payload = {
        "schema": "iii.build-record/v1",
        "source_identity": source_identity,
        "target_definition_id": target_definition_id,
        "policy_sha256": content_identity(policy),
        "components": sorted(set(components)),
        "requested_packages": requested,
        "impacted_packages": sorted(set(impacted_packages)),
        "packages": installed,
        "cache_keys": dict(sorted(cache_keys.items())),
        "cache": cache_value,
        "install_sha256": validation["install_sha256"],
        "assets": validation["assets"],
        "python": validation["python"],
        "elf": validation["elf"],
        "complete": True,
    }
    record = {"build_id": content_identity(payload), **payload}
    registry.validate("build-record", record)
    return record


def install_locked_wheels(
    wheelhouse: Path, site_packages: Path, lock: Mapping[str, Any]
) -> str:
    """Install locked wheel payloads without pip, dependency resolution, or network."""
    if lock.get("schema") != "iii.python-wheel-lock/v1" or lock.get("python_abi") != "cp312":
        raise ContractError("unsupported Python wheel lock")
    verify_wheelhouse(wheelhouse, lock)
    site_packages.mkdir(parents=True, exist_ok=True)
    if any(site_packages.iterdir()):
        raise ContractError("release-local site-packages destination is not empty")
    seen: set[str] = set()
    for wheel in lock.get("wheels", []):
        filename = wheel["filename"]
        if filename in seen or Path(filename).name != filename or not filename.endswith(".whl"):
            raise ContractError(f"invalid or duplicate locked wheel filename: {filename}")
        seen.add(filename)
        path = wheelhouse / filename
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != wheel["sha256"]:
            raise ContractError(f"wheel hash mismatch: {filename}")
        tags = wheel["tags"]
        try:
            parsed_tags = {tag for value in tags for tag in parse_tag(value)}
        except ValueError as exc:
            raise ContractError(f"wheel has an invalid compatibility tag: {filename}") from exc
        compatible = any(target_wheel_tag_compatible(tag) for tag in parsed_tags)
        if not tags or not compatible:
            raise ContractError(f"wheel target ABI is incompatible: {filename}")
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    relative = Path(member.filename)
                    if relative.is_absolute() or ".." in relative.parts or "\\" in member.filename:
                        raise ContractError(f"wheel path traversal: {filename}: {member.filename}")
                    parts = relative.parts
                    # PEP 427 assigns special meaning only to the top-level
                    # {distribution}-{version}.data directory. A nested resource
                    # directory whose name ends in .data is ordinary payload.
                    data_index = 0 if parts and parts[0].endswith(".data") else -1
                    if data_index >= 0:
                        if data_index + 1 >= len(parts) or parts[data_index + 1] not in {"purelib", "platlib"}:
                            if not member.is_dir():
                                raise ContractError(f"unsupported wheel data payload: {filename}: {member.filename}")
                            continue
                        relative = Path(*parts[data_index + 2:])
                    destination = site_packages / relative
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise ContractError(f"wheel file collision: {filename}: {member.filename}")
                    with archive.open(member) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    archived_mode = member.external_attr >> 16
                    destination.chmod(
                        0o755 if archived_mode & 0o111 else 0o644
                    )
        except zipfile.BadZipFile as exc:
            raise ContractError(f"invalid wheel archive: {filename}") from exc
    missing = sorted(set(lock.get("imports", [])) - {
        path.name for path in site_packages.iterdir()
        if path.is_dir() or path.suffix in {".py", ".so"}
    })
    if missing:
        raise ContractError("locked wheel imports are absent: " + ", ".join(missing))
    return hashlib.sha256(canonical_json(lock)).hexdigest()


def target_import_command(
    image: str, release_root: Path, imports: Iterable[str],
    activation_root: Path = Path("/opt/iii/current"),
) -> list[str]:
    modules = sorted(set(imports))
    if not modules:
        raise ContractError("Python import validation list is empty")
    code = "import " + ",".join(modules)
    return [
        "docker", "run", "--rm", "--network", "none", "--platform", "linux/arm64",
        "--entrypoint", f"{activation_root}/bin/iii-release-env",
        "-v", f"{release_root}:{activation_root}:ro", image,
        "/usr/bin/python3", "-c", code,
    ]


def target_elf_closure_command(
    image: str, release_root: Path,
    activation_root: Path = Path("/opt/iii/current"),
    host_library_prefixes: Iterable[str] = ("/opt/ros/jazzy",),
    host_elf_allowlist: Iterable[str] = (
        "ld-linux-aarch64.so.1", "libc.so.6", "libdl.so.2", "libgcc_s.so.1",
        "libm.so.6", "libpthread.so.0", "librt.so.1", "libstdc++.so.6",
        "libpython3.12.so.1.0",
    ),
) -> list[str]:
    root = str(activation_root)
    prefixes = sorted(set(host_library_prefixes))
    sonames = sorted(set(host_elf_allowlist))
    code = f"""import os,subprocess,sys
root={root!r}
allowed={prefixes!r}
allowed_sonames={sonames!r}
exact=['/lib/ld-linux-aarch64.so.1']
env=dict(os.environ)
for base,_,files in os.walk(root):
  for name in files:
    path=os.path.join(base,name)
    try:
      with open(path,'rb') as stream: elf=stream.read(4)==b'\x7fELF'
    except OSError: sys.exit(30)
    if not elf: continue
    result=subprocess.run(['/usr/bin/ldd',path],capture_output=True,text=True,env=env)
    output=result.stdout+result.stderr
    if result.returncode or 'not found' in output:
      print(path+'\\n'+output,file=sys.stderr);sys.exit(30)
    resolved=[]
    for line in output.splitlines():
      fields=line.strip().split()
      if '=>' in fields:
        value=fields[fields.index('=>')+1]
        if value.startswith('/'): resolved.append((fields[0],value))
      elif fields and fields[0].startswith('/'):
        resolved.append((os.path.basename(fields[0]),fields[0]))
    for soname,value in resolved:
      real=os.path.realpath(value)
      if value in exact or real in exact: continue
      if real==root or real.startswith(root+'/'): continue
      if soname in allowed_sonames and (real.startswith('/lib/') or real.startswith('/usr/lib/')): continue
      if not any(real==prefix or real.startswith(prefix+'/') for prefix in allowed):
        print(path+' resolves forbidden library '+value+' -> '+real,file=sys.stderr);sys.exit(30)
"""
    return [
        "docker", "run", "--rm", "--network", "none", "--platform", "linux/arm64",
        "--entrypoint", f"{activation_root}/bin/iii-release-env",
        "-v", f"{release_root}:{activation_root}:ro", image,
        "/usr/bin/python3", "-c", code,
    ]
