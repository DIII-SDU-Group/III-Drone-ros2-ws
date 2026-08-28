"""Retained local convergence for supported graphical ground-control hosts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform as platform_module
import pwd
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Mapping, Sequence

from iii_deployment.contracts import ContractError, ContractRegistry, content_identity

PLAN_SCHEMA = "iii.gc-provisioning-plan/v1"
REPORT_SCHEMA = "iii.gc-provisioning-report/v1"
POLICY_SCHEMA = "iii.gc-host-policy/v1"
CACHE_SCHEMA = "iii.gc-offline-cache/v1"
APPLICATION_PATHS = (
    "Dockerfile.cc",
    "deployment",
    "src/III-Drone-Contracts",
    "src/III-Drone-GC",
    "tools/III-Drone-CLI",
)
TREE_EXCLUDES = {
    ".ansible",
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "install",
    "log",
    "node_modules",
}
SUPPORTED_PLATFORM_IDS = {
    "ubuntu-22.04-x86_64",
    "ubuntu-24.04-x86_64",
}
OFFLINE_ROLES = {
    "ansible-controller-wheelhouse",
    "gc-runtime-wheelhouse",
    "arm64-builder-image",
    "apt-packages",
}
REQUIRED_GC_PATHS = {
    ".config/iii": "settings",
    ".config/iii/credentials": "secret",
    ".config/iii/identity": "machine-identity",
    ".config/iii/keys": "secret",
    ".config/iii/keys/signing": "secret",
    ".local/state/iii": "state",
    ".local/state/iii/captures": "portable-records",
    ".local/state/iii/logs": "logs",
    ".local/state/iii/registry": "portable-records",
    ".local/share/iii": "application",
    ".local/share/iii/gc-applications": "application",
    ".config/QGroundControl.org": "settings",
    ".local/share/QGroundControl": "state",
    "Documents/QGroundControl": "logs",
    ".cache/iii": "cache",
}
GC_LOGIN_UNITS = {
    "iii-gc-proxy.service",
    "iii-gc-frontend.service",
    "iii-gc-discovery.service",
    "iii-gc-mirror.service",
    "iii-gc-clock.service",
    "iii-gc-px4-parameters.service",
}


class GCHostError(ContractError):
    code = "III_GC_HOST_ERROR"


class GCHostChangedError(GCHostError):
    code = "III_GC_HOST_INPUT_CHANGED"


class GCHostDriftError(GCHostError):
    code = "III_GC_HOST_DRIFT"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_evidence(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise GCHostError(f"required input is not a real file: {path}")
    metadata = resolved.stat(follow_symlinks=False)
    return {
        "path": (
            resolved.relative_to(root.resolve()).as_posix() if root else str(resolved)
        ),
        "sha256": _sha256(resolved),
        "size": metadata.st_size,
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
    }


def _tree_manifest(path: Path) -> dict[str, Any]:
    root = path.resolve()
    if path.is_symlink() or root.is_symlink() or not root.is_dir():
        raise GCHostError(f"required input is not a real directory: {path}")
    files = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(root)
        if any(
            part in TREE_EXCLUDES
            or part.endswith(".egg-info")
            or part.endswith(".pyc")
            or part.endswith(".retry")
            for part in relative.parts
        ):
            continue
        if candidate.is_symlink():
            # Git submodule roots have a regular .git file and are handled as
            # independent repositories; source links are never accepted.
            raise GCHostError(f"input tree contains a symbolic link: {candidate}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise GCHostError(f"input tree contains a special file: {candidate}")
        files.append(_file_evidence(candidate, root=root))
    if not files:
        raise GCHostError(f"required input tree is empty: {path}")
    identity = content_identity({"files": files})
    return {"path": str(root), "content_id": identity, "files": files}


def _application_manifest(workspace: Path) -> dict[str, Any]:
    items = []
    for relative in APPLICATION_PATHS:
        path = workspace / relative
        if path.is_dir():
            value = _tree_manifest(path)
        else:
            value = _file_evidence(path)
        items.append({"locator": relative, "content": value})
    return {"items": items, "content_id": content_identity({"items": items})}


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise GCHostError(
            f"cannot authenticate repository {repo.name}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _git_identity(repo: Path, *, scoped_paths: Sequence[str] = ()) -> dict[str, Any]:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise GCHostError(f"repository is detached: {root}")
    arguments = ["status", "--porcelain=v1", "--untracked-files=all"]
    if scoped_paths:
        arguments.extend(["--", *scoped_paths])
    status = _git(root, *arguments)
    return {
        "repository": str(root),
        "branch": branch,
        "old_sha": _git(root, "rev-parse", "HEAD"),
        "new_sha": _git(root, "rev-parse", "HEAD"),
        "mutation": "none",
        "scoped_worktree_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "scoped_dirty": bool(status),
    }


OS_RELEASE_PATH = Path("/etc/os-release")
OS_RELEASE_CANONICAL_PATH = Path("/usr/lib/os-release")


def _read_os_release(path: Path) -> dict[str, str]:
    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise GCHostError("/etc/os-release is unavailable or unsafe") from exc
        if path != OS_RELEASE_PATH or resolved != OS_RELEASE_CANONICAL_PATH:
            raise GCHostError("/etc/os-release symlink target is not canonical")
        path = resolved
    if path.is_symlink() or not path.is_file():
        raise GCHostError("/etc/os-release is unavailable or unsafe")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def inspect_platform(
    policy: Mapping[str, Any],
    *,
    os_release_path: Path = OS_RELEASE_PATH,
    architecture: str | None = None,
) -> dict[str, Any]:
    release = _read_os_release(os_release_path)
    machine = architecture or platform_module.machine()
    aliases = {"amd64": "x86_64", "x86_64": "x86_64"}
    machine = aliases.get(machine, machine)
    match = next(
        (
            item
            for item in policy["supported_platforms"]
            if item["os_id"] == release.get("ID")
            and item["version_id"] == release.get("VERSION_ID")
            and item["architecture"] == machine
        ),
        None,
    )
    if match is None:
        raise GCHostError(
            "GC provisioning supports only graphical Ubuntu 22.04/24.04 x86_64"
        )
    return {
        "platform_id": match["id"],
        "os_id": release["ID"],
        "version_id": release["VERSION_ID"],
        "architecture": machine,
        "graphical_session_required": True,
        "excluded_prerequisites": list(policy["excluded_prerequisites"]),
    }


def load_policy(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    source = path.resolve()
    if path.is_symlink() or source.is_symlink() or not source.is_file():
        raise GCHostError("GC host policy must be a real regular file")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GCHostError(f"cannot load GC host policy: {exc}") from exc
    registry.validate("gc-host-policy", value)
    if value.get("schema") != POLICY_SCHEMA:
        raise GCHostError("unsupported GC host policy")
    if {item["id"] for item in value["supported_platforms"]} != SUPPORTED_PLATFORM_IDS:
        raise GCHostError("GC policy must define exactly both supported Ubuntu hosts")
    if set(value["offline_roles"]) != OFFLINE_ROLES:
        raise GCHostError("GC policy prepared-offline role inventory is not canonical")
    if len(value["managed_user_paths"]) != len(
        {item["path"] for item in value["managed_user_paths"]}
    ):
        raise GCHostError("GC policy managed user paths are duplicated")
    for item in value["managed_user_paths"]:
        relative = PurePosixPath(item["path"])
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or item["mode"] != "0700"
        ):
            raise GCHostError("GC policy managed user path is unsafe or non-private")
    declared_paths = {
        item["path"]: item["class"] for item in value["managed_user_paths"]
    }
    if any(
        declared_paths.get(path) != kind for path, kind in REQUIRED_GC_PATHS.items()
    ):
        raise GCHostError("GC policy omits a required persistent path boundary")
    if (
        set(value["login_units"]) != GC_LOGIN_UNITS
        or set(value["manual_units"]) != {"iii-gc-browser.service", "iii-qgc.service"}
        or value["recovery_units"] != ["iii-gc-application-reconcile.service"]
    ):
        raise GCHostError("GC policy user-session unit ownership is not canonical")
    if set(value["forbidden_proxy_host_packages"]) != {
        "mavsdk",
        "ros-*",
        "ros-jazzy-*",
        "cyclonedds-*",
    }:
        raise GCHostError("GC policy proxy package boundary is not canonical")
    packages = [*value["operational_packages"], *value["development_packages"]]
    if any(
        not isinstance(package, str)
        or not package
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789+.-"
            for character in package
        )
        for package in packages
    ):
        raise GCHostError("GC policy contains an unsafe package name")
    if any(
        package.startswith(("ros-", "cyclonedds")) or package == "mavsdk"
        for package in value["operational_packages"]
    ):
        raise GCHostError("GC operational policy crosses the ROS/DDS/MAVSDK boundary")
    definition = source.parents[1] / value["builder"]["definition"]
    if _sha256(definition) != value["builder"]["definition_sha256"]:
        raise GCHostError("pinned ARM64 builder definition differs from GC policy")
    return value


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise GCHostError("offline artifact path is malformed")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GCHostError("offline artifact path escapes its cache")
    return path


def _validate_cache_archive(path: Path, role: str) -> None:
    maximum_members = 100_000
    maximum_unpacked = min(
        100 * 1024**3,
        max(path.stat().st_size * 25, 64 * 1024**2),
    )
    count = 0
    unpacked = 0
    names = set()
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                count += 1
                if count > maximum_members:
                    raise GCHostError(f"offline {role} archive has too many members")
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or member.name in names
                    or member.issym()
                    or member.islnk()
                    or not (member.isfile() or member.isdir())
                ):
                    raise GCHostError(
                        f"offline {role} archive contains an unsafe member"
                    )
                names.add(member.name)
                unpacked += member.size
                if unpacked > maximum_unpacked:
                    raise GCHostError(
                        f"offline {role} archive exceeds its safe expansion bound"
                    )
    except (tarfile.TarError, OSError) as exc:
        raise GCHostError(
            f"offline {role} artifact is not a valid archive: {exc}"
        ) from exc
    if count == 0:
        raise GCHostError(f"offline {role} archive is empty")


def load_offline_cache(
    root: Path,
    *,
    platform_id: str,
    policy: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    cache_root = root.expanduser().resolve()
    if root.is_symlink() or cache_root.is_symlink() or not cache_root.is_dir():
        raise GCHostError("offline cache must be a real directory")
    manifest_path = cache_root / "gc-offline-cache.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise GCHostError("offline cache manifest is missing or unsafe")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GCHostError(f"cannot read offline cache manifest: {exc}") from exc
    registry.validate("gc-offline-cache", value)
    if value.get("schema") != CACHE_SCHEMA or value["platform_id"] != platform_id:
        raise GCHostError("offline cache platform differs from this GC host")
    unsigned = {key: item for key, item in value.items() if key != "cache_id"}
    if content_identity(unsigned) != value["cache_id"]:
        raise GCHostError("offline cache identity mismatch")
    roles = set()
    artifacts = []
    for item in value["artifacts"]:
        if item["role"] in roles:
            raise GCHostError(f"offline cache role is duplicated: {item['role']}")
        relative = _safe_relative(item["path"])
        path = cache_root.joinpath(*relative.parts)
        current = cache_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise GCHostError(f"offline cache artifact path is linked: {relative}")
        evidence = _file_evidence(path, root=cache_root)
        if evidence["path"] != relative.as_posix():
            raise GCHostError(f"offline cache artifact path changed: {relative}")
        if evidence["sha256"] != item["sha256"] or evidence["size"] != item["size"]:
            raise GCHostError(f"offline cache artifact changed: {relative}")
        _validate_cache_archive(path, item["role"])
        roles.add(item["role"])
        artifacts.append({**item, "absolute_path": str(path.resolve())})
    expected_roles = set(policy["offline_roles"])
    missing = sorted(expected_roles - roles)
    unexpected = sorted(roles - expected_roles)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise GCHostError("offline cache role inventory differs: " + "; ".join(details))
    return {
        "root": str(cache_root),
        "manifest": _file_evidence(manifest_path),
        "cache_id": value["cache_id"],
        "platform_id": value["platform_id"],
        "artifacts": artifacts,
    }


def _current_user(user: str | None, home: Path | None) -> dict[str, Any]:
    uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
    record = pwd.getpwuid(uid)
    name = user or record.pw_name
    selected_home = (home or Path(record.pw_dir)).expanduser().resolve()
    if name != record.pw_name or selected_home != Path(record.pw_dir).resolve():
        raise GCHostError("GC provisioning may target only the invoking graphical user")
    if selected_home.is_symlink() or not selected_home.is_dir():
        raise GCHostError("GC user home must be a real directory")
    return {"name": name, "uid": uid, "gid": record.pw_gid, "home": str(selected_home)}


def _replacement_preflight(user: Mapping[str, Any]) -> None:
    home = Path(str(user["home"]))
    sensitive = (
        home / ".config/iii/identity/machine-id",
        home / ".config/iii/keys/ssh/id_ed25519",
        home / ".config/iii/credentials/runtime-api.token",
    )
    if any(path.exists() or path.is_symlink() for path in sensitive):
        raise GCHostError(
            "replacement provisioning requires a fresh host without prior machine credentials"
        )


def build_plan(
    *,
    operation_id: str,
    workspace: Path,
    policy_path: Path,
    schema_root: Path,
    ansible_root: Path,
    ansible_playbook: Path,
    offline: bool = False,
    offline_cache: Path | None = None,
    replacement_archive: Path | None = None,
    user: str | None = None,
    home: Path | None = None,
    os_release_path: Path = OS_RELEASE_PATH,
    architecture: str | None = None,
) -> dict[str, Any]:
    registry = ContractRegistry(schema_root)
    policy = load_policy(policy_path, registry)
    platform = inspect_platform(
        policy, os_release_path=os_release_path, architecture=architecture
    )
    account = _current_user(user, home)
    root = workspace.resolve()
    if workspace.is_symlink() or root.is_symlink() or not (root / ".git").exists():
        raise GCHostError("GC provisioning requires this workspace clone")
    playbook = ansible_playbook.resolve()
    if ansible_playbook.is_symlink() or not playbook.is_file():
        raise GCHostError("ansible-playbook executable is missing or unsafe")
    cache = None
    if offline:
        if offline_cache is None:
            raise GCHostError("prepared-offline provisioning requires --offline-cache")
        cache = load_offline_cache(
            offline_cache,
            platform_id=platform["platform_id"],
            policy=policy,
            registry=registry,
        )
    elif offline_cache is not None:
        raise GCHostError("offline cache is accepted only with prepared-offline mode")
    archive_import = None
    replacement = replacement_archive is not None
    if replacement:
        _replacement_preflight(account)
        from iii.registry import build_import_plan

        archive_import = build_import_plan(
            Path(account["home"]) / ".local/state/iii/registry",
            archive_path=replacement_archive,
        )
        if archive_import["conflicts"] or archive_import["missing_blob_ids"]:
            raise GCHostError("replacement archive is incomplete or conflicts locally")
    scoped = list(APPLICATION_PATHS) + ["deps/submodule-lock.txt"]
    repositories = [
        _git_identity(root, scoped_paths=scoped),
        _git_identity(root / "tools/III-Drone-CLI"),
        _git_identity(root / "src/III-Drone-GC"),
    ]
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "operation_id": operation_id,
        "platform": platform,
        "user": account,
        "workspace": {
            "path": str(root),
            "application_inputs": _application_manifest(root),
            "submodule_lock": _file_evidence(root / "deps/submodule-lock.txt"),
        },
        "policy": {"path": str(policy_path.resolve()), **_file_evidence(policy_path)},
        "ansible": _tree_manifest(ansible_root),
        "ansible_playbook": _file_evidence(playbook),
        "repositories": repositories,
        "offline": offline,
        "offline_cache": cache,
        "replacement": replacement,
        "archive_import": archive_import,
        "required_checks": [
            "supported-platform-and-graphical-user",
            "exact-source-policy-and-cache-reauthentication",
            "first-convergence",
            "second-run-zero-drift",
            "ROS-free-proxy-boundary",
            "login-lifecycle-and-fixed-iii.local-discovery",
            "fresh-identity-and-secret-exclusion",
        ],
        "declared_permissions": [
            "local-passwordless-sudo-cache",
            "host-package-and-docker-convergence",
            "current-user-group-membership",
            "current-user-service-and-path-convergence",
        ],
        "mutations": [
            "install GC operational and development host packages",
            "install/build pinned frontend, proxy, companions, and ARM64 builder",
            "install graphical-session user services and desktop launcher",
            "create fresh local machine identity and SSH key only when absent",
            "import verified non-secret portable records before replacement enrollment",
        ],
        "excluded_prerequisites": list(policy["excluded_prerequisites"]),
        "managed_boundaries": {
            "operational": "packages, declared paths, user services, discovery and companions",
            "application": "pinned GC containers and repository-managed runtime environment",
            "development": "strict Git/submodules, Ansible controller, offline cache and ARM64 builder",
            "unmanaged_user_state": "preserved and excluded from convergence/drift",
        },
    }
    plan["content_id"] = content_identity(plan)
    registry.validate("gc-provisioning-plan", plan)
    return plan


def _verify_replacement_converged(plan: Mapping[str, Any]) -> None:
    from iii.registry import build_import_plan

    expected = plan["archive_import"]
    root = Path(str(plan["user"]["home"])) / ".local/state/iii/registry"
    current = build_import_plan(root, archive_path=Path(str(expected["archive_path"])))
    immutable_fields = (
        "archive_path",
        "archive_sha256",
        "archive_size",
        "archive_manifest",
        "record_count",
        "cross_computer_safe",
    )
    if any(current[field] != expected[field] for field in immutable_fields):
        raise GCHostChangedError("replacement archive changed after import")
    expected_files = sorted(
        item["locator"]
        for record in expected["archive_manifest"]["records"]
        for item in record["files"]
    )
    if (
        current["conflicts"]
        or current["missing_blob_ids"]
        or current["idempotent_locators"] != expected_files
    ):
        raise GCHostChangedError("replacement archive import is incomplete or changed")
    for record in expected["archive_manifest"]["records"]:
        directories = list(record["directories"])
        if record["unit_kind"] == "directory":
            directories.append(record["locator"])
        for locator in directories:
            path = root.joinpath(*PurePosixPath(locator).parts)
            if path.is_symlink() or not path.is_dir():
                raise GCHostChangedError(
                    "replacement archive directory import is incomplete or unsafe"
                )

    home = Path(str(plan["user"]["home"]))
    uid = int(plan["user"]["uid"])
    required = (
        (home / ".config/iii/identity/machine-id", 0o600),
        (home / ".config/iii/keys/ssh/id_ed25519", 0o600),
        (home / ".config/iii/keys/ssh/id_ed25519.pub", 0o644),
    )
    for path, mode in required:
        if path.is_symlink() or not path.is_file():
            raise GCHostChangedError("replacement fresh machine material is incomplete")
        metadata = path.stat(follow_symlinks=False)
        if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != mode:
            raise GCHostChangedError(
                "replacement fresh machine material has unsafe ownership or mode"
            )
    credential = home / ".config/iii/credentials/runtime-api.token"
    if credential.exists() or credential.is_symlink():
        raise GCHostChangedError(
            "replacement runtime enrollment must remain fresh and explicit"
        )


def _verify_plan(
    plan: Mapping[str, Any],
    *,
    schema_root: Path,
    replacement_state: str = "fresh",
) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = ContractRegistry(schema_root)
    registry.validate("gc-provisioning-plan", plan)
    if plan.get("schema") != PLAN_SCHEMA:
        raise GCHostChangedError("unsupported retained GC provisioning plan")
    unsigned = {key: item for key, item in plan.items() if key != "content_id"}
    if content_identity(unsigned) != plan.get("content_id"):
        raise GCHostChangedError("retained GC provisioning plan identity mismatch")
    policy_path = Path(str(plan["policy"]["path"]))
    policy = load_policy(policy_path, registry)
    current_platform = inspect_platform(policy)
    if current_platform != plan["platform"]:
        raise GCHostChangedError("GC platform changed after planning")
    current_user = _current_user(None, None)
    if current_user != plan["user"]:
        raise GCHostChangedError("GC user identity changed after planning")
    workspace = Path(str(plan["workspace"]["path"]))
    if _application_manifest(workspace) != plan["workspace"]["application_inputs"]:
        raise GCHostChangedError("GC application inputs changed after planning")
    if (
        _file_evidence(workspace / "deps/submodule-lock.txt")
        != plan["workspace"]["submodule_lock"]
    ):
        raise GCHostChangedError("submodule lock changed after planning")
    if _tree_manifest(Path(str(plan["ansible"]["path"]))) != plan["ansible"]:
        raise GCHostChangedError("GC Ansible project changed after planning")
    if (
        _file_evidence(Path(str(plan["ansible_playbook"]["path"])))
        != plan["ansible_playbook"]
    ):
        raise GCHostChangedError("ansible-playbook executable changed after planning")
    for expected in plan["repositories"]:
        current = _git_identity(Path(expected["repository"]))
        # Workspace identity was scoped at plan time. Recompute using the same
        # exact paths rather than unrelated user work elsewhere in the tree.
        if Path(expected["repository"]) == workspace:
            current = _git_identity(
                workspace,
                scoped_paths=list(APPLICATION_PATHS) + ["deps/submodule-lock.txt"],
            )
        if current != expected:
            raise GCHostChangedError("repository branch, SHA, or scoped state changed")
    cache = None
    if plan["offline"]:
        cache = load_offline_cache(
            Path(str(plan["offline_cache"]["root"])),
            platform_id=plan["platform"]["platform_id"],
            policy=policy,
            registry=registry,
        )
        if cache != plan["offline_cache"]:
            raise GCHostChangedError("prepared offline cache changed after planning")
    if plan["replacement"]:
        if replacement_state not in {"fresh", "converged", "fresh-or-converged"}:
            raise GCHostChangedError("replacement verification state is invalid")
        if replacement_state in {"fresh", "fresh-or-converged"}:
            try:
                _replacement_preflight(plan["user"])
                from iii.registry import build_import_plan

                current_import = build_import_plan(
                    Path(str(plan["user"]["home"])) / ".local/state/iii/registry",
                    archive_path=Path(str(plan["archive_import"]["archive_path"])),
                )
                if current_import != plan["archive_import"]:
                    raise GCHostChangedError(
                        "replacement archive or destination changed"
                    )
            except GCHostError:
                if replacement_state != "fresh-or-converged":
                    raise
                _verify_replacement_converged(plan)
        else:
            _verify_replacement_converged(plan)
    return policy, cache or {}


def _run_ansible(
    plan: Mapping[str, Any],
    *,
    check: bool,
    test_mode: bool = False,
) -> dict[str, Any]:
    project = Path(str(plan["ansible"]["path"]))
    with tempfile.TemporaryDirectory(prefix="iii-gc-provision-") as temporary:
        scratch = Path(temporary)
        result_path = scratch / "result.json"
        extra_vars = scratch / "extra-vars.json"
        extra = {
            "iii_gc_user": plan["user"]["name"],
            "iii_gc_uid": plan["user"]["uid"],
            "iii_gc_gid": plan["user"]["gid"],
            "iii_gc_home": plan["user"]["home"],
            "iii_gc_workspace": plan["workspace"]["path"],
            "iii_gc_platform_id": plan["platform"]["platform_id"],
            "iii_gc_offline": plan["offline"],
            "iii_gc_offline_cache": (
                plan["offline_cache"]["root"] if plan["offline_cache"] else ""
            ),
            "iii_gc_policy": str(plan["policy"]["path"]),
            "iii_gc_application_id": plan["workspace"]["application_inputs"][
                "content_id"
            ],
            "iii_gc_cache_id": (
                plan["offline_cache"]["cache_id"] if plan["offline_cache"] else "online"
            ),
            "iii_gc_offline_artifacts": (
                {
                    item["role"]: item["absolute_path"]
                    for item in plan["offline_cache"]["artifacts"]
                }
                if plan["offline_cache"]
                else {}
            ),
            "iii_gc_test_mode": test_mode,
        }
        extra["iii_gc_install_id"] = hashlib.sha256(
            (
                extra["iii_gc_application_id"] + ":" + str(extra["iii_gc_cache_id"])
            ).encode("ascii")
        ).hexdigest()
        extra_vars.write_bytes(_canonical(extra) + b"\n")
        os.chmod(extra_vars, 0o600)
        argv = [
            str(plan["ansible_playbook"]["path"]),
            "--inventory",
            "localhost,",
            "--connection",
            "local",
            "--extra-vars",
            "@" + str(extra_vars),
            "--diff",
        ]
        if check:
            argv.append("--check")
        argv.append(str(project / "playbooks/gc-converge.yml"))
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
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-60:])
            raise GCHostError(
                f"GC Ansible convergence failed with {completed.returncode}:\n{tail}"
            )
        if result_path.is_symlink() or not result_path.is_file():
            raise GCHostError("GC Ansible convergence emitted no authenticated recap")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        ContractRegistry(project.parent / "schemas/v1").validate(
            "ansible-run-result", result
        )
        if result["check_mode"] is not check:
            raise GCHostError("GC Ansible recap has the wrong check-mode identity")
        if result["totals"]["failures"] or result["totals"]["unreachable"]:
            raise GCHostError("GC Ansible recap reports failed or unreachable hosts")
        return result


def check_plan(plan: Mapping[str, Any], *, schema_root: Path) -> dict[str, Any]:
    _verify_plan(plan, schema_root=schema_root, replacement_state="fresh-or-converged")
    return _run_ansible(plan, check=True)


def _sudo_ready() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return
    completed = subprocess.run(
        ["sudo", "-n", "true"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise GCHostError(
            "prime the local sudo credential cache with 'sudo -v' before apply; "
            "passwords are never accepted in arguments, files, or retained plans"
        )


def _category_drift(recap: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = recap.get("categories", {}).get(name, {})
    return {
        "managed": True,
        "changed": int(value.get("changed", 0)),
        "failures": int(value.get("failures", 0)),
    }


def apply_plan(plan: Mapping[str, Any], *, schema_root: Path) -> dict[str, Any]:
    _verify_plan(plan, schema_root=schema_root, replacement_state="fresh-or-converged")
    _sudo_ready()
    archive_receipt = None
    if plan["replacement"]:
        from iii.registry import apply_import_plan

        archive_receipt = apply_import_plan(
            Path(str(plan["user"]["home"])) / ".local/state/iii/registry",
            plan["archive_import"],
        )
    first = _run_ansible(plan, check=False)
    # Reauthenticate every retained byte after the first mutation before using
    # the same plan for the zero-drift proof.
    _verify_plan(plan, schema_root=schema_root, replacement_state="converged")
    second = _run_ansible(plan, check=True)
    if second["totals"]["changed"] != 0:
        raise GCHostDriftError(
            f"second GC convergence predicts {second['totals']['changed']} unintended change(s)"
        )
    home = Path(str(plan["user"]["home"]))
    machine_id = home / ".config/iii/identity/machine-id"
    ssh_public = home / ".config/iii/keys/ssh/id_ed25519.pub"
    if not machine_id.is_file() or machine_id.is_symlink():
        raise GCHostError("GC convergence did not create a fresh machine identity")
    if not ssh_public.is_file() or ssh_public.is_symlink():
        raise GCHostError("GC convergence did not create a fresh SSH public key")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "operation_id": plan["operation_id"],
        "plan_content_id": plan["content_id"],
        "platform_id": plan["platform"]["platform_id"],
        "state": "provisioned",
        "runs": {"first_convergence": first, "idempotence_check": second},
        "drift": {
            "operational": _category_drift(second, "operational"),
            "development": _category_drift(second, "development"),
            "application": _category_drift(second, "application"),
            "unmanaged_user_state": {
                "managed": False,
                "changed": None,
                "reason": "outside declared GC user paths and intentionally preserved",
            },
        },
        "archive_import": archive_receipt,
        "fresh_identity": {
            "machine_identity_sha256": _sha256(machine_id),
            "ssh_public_key_sha256": _sha256(ssh_public),
            "private_material_exported": False,
            "runtime_enrollment_required": True,
        },
    }
    report["report_id"] = content_identity(report)
    ContractRegistry(schema_root).validate("gc-provisioning-report", report)
    return report


def inspect_status(
    *,
    policy_path: Path,
    schema_root: Path,
    home: Path | None = None,
    runner=subprocess.run,
) -> dict[str, Any]:
    registry = ContractRegistry(schema_root)
    policy = load_policy(policy_path, registry)
    platform = inspect_platform(policy)
    account = _current_user(None, home)
    root = Path(account["home"])
    paths = []
    for item in policy["managed_user_paths"]:
        path = root / item["path"]
        exists = path.exists() and not path.is_symlink()
        mode = format(stat.S_IMODE(path.stat().st_mode), "04o") if exists else None
        paths.append({**item, "exists": exists, "actual_mode": mode})
    units = []
    for unit in [
        *policy["login_units"],
        *policy["manual_units"],
        *policy["recovery_units"],
    ]:
        completed = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,UnitFileState",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        values = {}
        if completed.returncode == 0:
            for line in completed.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
        units.append(
            {
                "unit": unit,
                "load_state": values.get("LoadState") or "unavailable",
                "active_state": values.get("ActiveState") or "unavailable",
                "unit_file_state": values.get("UnitFileState") or "unavailable",
            }
        )
    status: dict[str, Any] = {
        "schema": "iii.gc-host-status/v1",
        "platform": platform,
        "paths": paths,
        "units": units,
        "machine_identity": (root / ".config/iii/identity/machine-id").is_file(),
        "ssh_key": (root / ".config/iii/keys/ssh/id_ed25519").is_file(),
    }
    status["status_id"] = content_identity(status)
    registry.validate("gc-host-status", status)
    return status
