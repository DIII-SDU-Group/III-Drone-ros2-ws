"""Offline documentation inventory, ownership, and drift validation."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


MANIFEST_SCHEMA = "iii.documentation-manifest/v1"
POLICY_SCHEMA = "iii.documentation-policy/v1"
DOC_SUFFIXES = (".md", ".rst")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")


class DocumentationError(ValueError):
    pass


@dataclass(frozen=True)
class Repository:
    id: str
    path: Path


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentationError(f"invalid documentation policy: {exc}") from exc
    if policy.get("schema") != POLICY_SCHEMA:
        raise DocumentationError(f"unsupported documentation policy schema: {policy.get('schema')!r}")
    if not policy.get("repositories"):
        raise DocumentationError("documentation policy has no repositories")
    return policy


def _git_files(repo: Path) -> tuple[str, ...]:
    process = subprocess.run(
        [
            "git", "-C", str(repo), "ls-files", "--cached", "--others",
            "--exclude-standard", "*.md", "*.rst",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise DocumentationError(f"cannot inventory {repo}: {process.stderr.strip()}")
    return tuple(sorted(line for line in process.stdout.splitlines() if line))


def _excluded(path: str, patterns: Iterable[dict[str, str]]) -> tuple[bool, str | None]:
    for entry in patterns:
        if fnmatch.fnmatch(path, entry["pattern"]):
            return True, entry["reason"]
    return False, None


def _classification(path: str) -> tuple[str, str, bool, bool]:
    name = Path(path).name.lower()
    if path == "AGENTS.md" or path.startswith("docs/agents/"):
        return ("contextual-design", "agents", False, True)
    if path == "CONTEXT.md" or path.endswith("/CONTEXT.md"):
        return ("contextual-design", "engineering", True, True)
    if path == "CONTEXT-MAP.md":
        return ("canonical", "engineering", True, True)
    if "/adr/" in f"/{path}":
        return ("adr", "engineering", True, True)
    if path.startswith("codex-backlogs/") or "backlog" in name or "plan" in name:
        return ("historical-record", "engineering", False, False)
    if path.startswith("docs/") and ("operation" in name or "testing" in name):
        return ("runbook", "operator", True, True)
    if name == "readme.md":
        return ("canonical", "mixed", True, True)
    return ("contextual-design", "engineering", False, True)


def materialize_manifest(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    exclusions = policy.get("exclusions", [])
    for repo_data in policy["repositories"]:
        repo = Repository(repo_data["id"], root / repo_data["path"])
        if not repo.path.is_dir():
            raise DocumentationError(f"governed repository is missing: {repo_data['path']}")
        for path in _git_files(repo.path):
            is_excluded, reason = _excluded(path, exclusions if repo.id == "workspace" else ())
            classification, audience, canonical, release_include = _classification(path)
            entries.append(
                {
                    "repository": repo.id,
                    "repository_path": repo_data["path"],
                    "path": path,
                    "owner": repo.id,
                    "context": repo.id if repo.id != "workspace" else "workspace-integration",
                    "audience": audience,
                    "classification": "excluded" if is_excluded else classification,
                    "canonical": False if is_excluded else canonical,
                    "lifecycle": "excluded" if is_excluded else ("maintained" if classification != "historical-record" else "historical"),
                    "source_of_truth": path,
                    "generated": False,
                    "qualified_release_inclusion": False if is_excluded else release_include,
                    "exclusion_reason": reason,
                }
            )
    entries.sort(key=lambda row: (row["repository"], row["path"]))
    return {
        "schema": MANIFEST_SCHEMA,
        "policy_schema": policy["schema"],
        "canonical_roots": policy.get("canonical_roots", []),
        "documents": entries,
    }


def _local_link_errors(file_path: Path, logical_path: str) -> list[str]:
    errors: list[str] = []
    text = file_path.read_text(encoding="utf-8")
    for match in MARKDOWN_LINK.finditer(text):
        raw = match.group("target").strip()
        if raw.startswith(("http://", "https://", "mailto:", "#", "app://")):
            continue
        target = raw.split("#", 1)[0].strip("<>")
        if not target:
            continue
        resolved = (file_path.parent / target).resolve()
        if not resolved.exists():
            errors.append(f"{logical_path}: broken local link {raw!r}")
    return errors


def audit_manifest(root: Path, policy: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return [f"unsupported documentation manifest schema: {manifest.get('schema')!r}"]
    expected = materialize_manifest(root, policy)
    if manifest != expected:
        expected_keys = {(row["repository"], row["path"]) for row in expected["documents"]}
        actual_keys = {
            (row.get("repository"), row.get("path"))
            for row in manifest.get("documents", [])
            if isinstance(row, dict)
        }
        for key in sorted(expected_keys - actual_keys):
            errors.append(f"documentation manifest is missing {key[0]}:{key[1]}")
        for key in sorted(actual_keys - expected_keys):
            errors.append(f"documentation manifest has stale entry {key[0]}:{key[1]}")
        if not errors:
            errors.append("documentation manifest metadata differs from policy-derived inventory")
    seen: set[tuple[str, str]] = set()
    for entry in manifest.get("documents", []):
        key = (entry["repository"], entry["path"])
        if key in seen:
            errors.append(f"duplicate documentation entry {key[0]}:{key[1]}")
            continue
        seen.add(key)
        if entry["lifecycle"] != "maintained":
            continue
        repository_root = root / entry["repository_path"]
        file_path = repository_root / entry["path"]
        if file_path.suffix == ".md":
            errors.extend(_local_link_errors(file_path, f"{key[0]}:{key[1]}"))
        content = file_path.read_text(encoding="utf-8")
        for term in policy.get("forbidden_current_terms", []):
            if term in content:
                errors.append(f"{key[0]}:{key[1]}: forbidden current term {term!r}")
    return errors


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentationError(f"invalid documentation manifest: {exc}") from exc
