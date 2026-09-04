"""Deterministic clean/dirty source capture and component-impact analysis."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity


def _git(repo: Path, *arguments: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, check=False
    )
    if process.returncode:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise ContractError(detail or f"git {' '.join(arguments)} failed in {repo}")
    return process.stdout


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
        for pattern in patterns
    )


def load_source_policy(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load source policy: {exc}") from exc
    registry.validate("source-policy", value)
    if value["governed_repositories"][0] != ".":
        raise ContractError("source policy must list the workspace first")
    return value


def _entry(repo: Path, path: str) -> dict[str, Any]:
    candidate = repo / path
    try:
        if candidate.is_symlink():
            target = os.readlink(candidate)
            if os.path.isabs(target) or ".." in PurePosixPath(target).parts:
                raise ContractError(f"unsafe source symlink {path} -> {target}")
            return {"path": path, "kind": "symlink", "sha256": _sha256(target.encode())}
        if not candidate.exists():
            return {"path": path, "kind": "deleted", "sha256": None}
        if not candidate.is_file():
            raise ContractError(f"ambiguous non-regular source path: {path}")
        return {"path": path, "kind": "file", "sha256": _sha256(candidate.read_bytes())}
    except OSError as exc:
        raise ContractError(f"cannot capture source path {path}: {exc}") from exc


def _tracked_paths(repo: Path) -> list[str]:
    paths: list[str] = []
    for record in _git(repo, "ls-files", "-s", "-z").split(b"\0"):
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        if mode == b"160000":
            continue
        paths.append(encoded_path.decode("utf-8", "surrogateescape"))
    return sorted(paths)


def _workspace_relevant(path: str, policy: Mapping[str, Any]) -> bool:
    if path in policy["workspace_source_files"]:
        return True
    return any(path == root or path.startswith(root + "/") for root in policy["workspace_source_roots"])


def _exclusion_reason(path: str, policy: Mapping[str, Any]) -> str | None:
    lowered = path.lower()
    if _matches(path, policy["excluded_path_patterns"]):
        if any(token in lowered for token in ("secret", "credential", ".env", ".key", ".pem", ".p12")):
            return "sensitive"
        if any(token in lowered for token in ("dataset", "rosbag", ".bag", ".db3", ".mcap")):
            return "dataset"
        return "generated"
    return None


def _changed(repo: Path) -> tuple[list[str], list[str]]:
    tracked = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in _git(repo, "diff", "--name-only", "-z", "HEAD", "--").split(b"\0")
        if path
    )
    untracked = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if path
    )
    return tracked, untracked


def _capture_repository(
    root: Path, repo_path: str, policy: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    repo = root if repo_path == "." else root / repo_path
    if not repo.is_dir() or not (repo / ".git").exists():
        raise ContractError(f"governed repository is missing or uninitialized: {repo_path}")
    if _git(repo, "ls-files", "-u", "-z"):
        raise ContractError(f"governed repository has ambiguous unmerged entries: {repo_path}")
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    tracked_changed, untracked_candidates = _changed(repo)
    # A workspace source snapshot is deliberately scoped by the source policy.
    # The workspace also carries adjacent research tooling, which must not make
    # a deployable field build dirty or alter its identity.  Governed
    # subrepositories, on the other hand, are captured in full.  Tracked source
    # entries are represented only by hashes; their bytes never enter the
    # provenance record.
    tracked_paths = _tracked_paths(repo)
    if repo_path == ".":
        tracked_paths = [path for path in tracked_paths if _workspace_relevant(path, policy)]
        tracked_changed = [path for path in tracked_changed if _workspace_relevant(path, policy)]
    tracked_entries = [_entry(repo, path) for path in tracked_paths]
    untracked_entries: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    changed_paths = [f"{repo_path}/{path}" if repo_path != "." else path for path in tracked_changed]

    for path in untracked_candidates:
        reason = _exclusion_reason(path, policy)
        relevant = repo_path != "." or _workspace_relevant(path, policy)
        qualified = f"{repo_path}/{path}" if repo_path != "." else path
        if reason is not None:
            excluded.append({"path": qualified, "reason": reason})
        elif not relevant:
            excluded.append({"path": qualified, "reason": "unrelated"})
        else:
            untracked_entries.append(_entry(repo, path))
            changed_paths.append(qualified)

    patch = _git(repo, "diff", "--binary", "HEAD", "--")
    tracked_patch = _sha256(patch) if patch else None
    state = "modified" if tracked_changed else ("untracked" if untracked_entries else "clean")
    repository_identity = content_identity({
        "entries": tracked_entries,
        "untracked": untracked_entries,
    })
    return ({
        "path": repo_path,
        "commit": commit,
        "state": state,
        "content_identity": repository_identity,
        "tracked_patch_sha256": tracked_patch,
        "entries": tracked_entries,
        "untracked": untracked_entries,
    }, excluded, sorted(set(changed_paths)))


def analyze_component_impact(
    changed_paths: Iterable[str], policy: Mapping[str, Any]
) -> dict[str, Any]:
    causes: dict[str, list[str]] = {}
    unclassified: list[str] = []
    for path in sorted(set(changed_paths)):
        matches = [
            rule for rule in policy["component_rules"]
            if _matches(path, rule["patterns"])
        ]
        if not matches:
            if _matches(path, policy["non_artifact_patterns"]):
                continue
            unclassified.append(path)
            continue
        for rule in matches:
            for component in rule["components"]:
                causes.setdefault(component, []).append(f"{rule['id']}: {path}")
    if unclassified:
        raise ContractError("ambiguous component impact for: " + ", ".join(unclassified))
    return {
        "components": sorted(causes),
        "causes": {component: sorted(set(values)) for component, values in sorted(causes.items())},
    }


def validate_component_selection(required: Mapping[str, Any], requested: Sequence[str]) -> None:
    unknown = sorted(set(requested) - {"drone", "gc"})
    if unknown:
        raise ContractError("unknown requested component: " + ", ".join(unknown))
    missing = sorted(set(required["components"]) - set(requested))
    if missing:
        details = "; ".join(
            f"{component}: {', '.join(required['causes'][component])}" for component in missing
        )
        raise ContractError("unsafe manual component omission: " + details)


def _snapshot_identity(snapshot: Mapping[str, Any]) -> str:
    return content_identity({
        "policy_sha256": snapshot["policy_sha256"],
        "dependency_lock_sha256": snapshot["dependency_lock_sha256"],
        "repositories": [
            {"path": repo["path"], "content_identity": repo["content_identity"]}
            for repo in snapshot["repositories"]
        ],
    })


def capture_source_snapshot(
    root: Path, policy: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    root = root.resolve()
    lock_path = root / "deps/submodule-lock.txt"
    if not lock_path.is_file():
        raise ContractError("dependency lock is missing")
    repositories: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    changed_paths: list[str] = []
    for repo_path in policy["governed_repositories"]:
        repository, repo_excluded, repo_changed = _capture_repository(root, repo_path, policy)
        repositories.append(repository)
        excluded.extend(repo_excluded)
        changed_paths.extend(repo_changed)
    snapshot: dict[str, Any] = {
        "schema": "iii.source-snapshot/v1",
        "content_identity": "0" * 64,
        "policy_sha256": content_identity(policy),
        "workspace_commit": repositories[0]["commit"],
        "branch": _git(root, "branch", "--show-current").decode().strip() or "DETACHED",
        "clean": all(repo["state"] == "clean" for repo in repositories),
        "dependency_lock_sha256": _sha256(lock_path.read_bytes()),
        "repositories": repositories,
        "excluded": sorted(excluded, key=lambda item: (item["path"], item["reason"])),
        "changed_paths": sorted(set(changed_paths)),
        "impact": analyze_component_impact(changed_paths, policy),
    }
    snapshot["content_identity"] = _snapshot_identity(snapshot)
    registry.validate("source-snapshot", snapshot)
    return snapshot


def verify_source_snapshot(snapshot: Mapping[str, Any], registry: ContractRegistry) -> None:
    registry.validate("source-snapshot", snapshot)
    if snapshot["content_identity"] != _snapshot_identity(snapshot):
        raise ContractError("source snapshot content identity mismatch")
    expected_clean = all(repo["state"] == "clean" for repo in snapshot["repositories"])
    if snapshot["clean"] != expected_clean:
        raise ContractError("source snapshot clean classification mismatch")


def release_manifest_source(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    root = snapshot["repositories"][0]
    report = provenance_markdown(snapshot).encode()
    return {
        "workspace_commit": snapshot["workspace_commit"],
        "branch": snapshot["branch"],
        "clean": snapshot["clean"],
        "content_identity": snapshot["content_identity"],
        "snapshot_content_identity": snapshot["content_identity"],
        "snapshot_sha256": _sha256(canonical_json(snapshot)),
        "provenance_report_sha256": _sha256(report),
        "tracked_patch_sha256": root["tracked_patch_sha256"],
        "untracked": [
            {"path": entry["path"], "sha256": entry["sha256"]}
            for entry in root["untracked"]
            if entry["sha256"] is not None
        ],
        "submodules": [
            {
                "path": repo["path"],
                "commit": repo["commit"],
                "state": repo["state"],
                "content_identity": repo["content_identity"],
            }
            for repo in snapshot["repositories"][1:]
        ],
    }


def provenance_markdown(snapshot: Mapping[str, Any]) -> str:
    lines = [
        "# Field-development source provenance",
        "",
        f"- Source identity: `{snapshot['content_identity']}`",
        f"- Workspace commit: `{snapshot['workspace_commit']}`",
        f"- Branch: `{snapshot['branch']}`",
        f"- Dependency lock SHA-256: `{snapshot['dependency_lock_sha256']}`",
        f"- Classification: `{'clean' if snapshot['clean'] else 'dirty'}`",
        f"- Required artifacts: `{', '.join(snapshot['impact']['components']) or 'none'}`",
        "",
        "## Repository inventory",
        "",
        "| Repository | Commit | State | Content identity |",
        "|---|---|---|---|",
    ]
    for repo in snapshot["repositories"]:
        lines.append(
            f"| `{repo['path']}` | `{repo['commit']}` | `{repo['state']}` | `{repo['content_identity']}` |"
        )
    lines.extend(["", "## Changed source and impact", ""])
    if snapshot["changed_paths"]:
        for path in snapshot["changed_paths"]:
            lines.append(f"- `{path}`")
    else:
        lines.append("- No governed working-tree changes.")
    for component in snapshot["impact"]["components"]:
        lines.extend(["", f"### {component}", ""])
        lines.extend(f"- {cause}" for cause in snapshot["impact"]["causes"][component])
    lines.extend(["", "## Explicit exclusions", ""])
    if snapshot["excluded"]:
        lines.extend(f"- `{item['path']}` — {item['reason']}" for item in snapshot["excluded"])
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"
