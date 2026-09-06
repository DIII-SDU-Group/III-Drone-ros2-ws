"""Offline documentation inventory, ownership, routing, and drift validation."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any, Iterable, Mapping
from urllib.parse import unquote

from iii_deployment.contracts import content_identity

MANIFEST_SCHEMA = "iii.documentation-manifest/v1"
POLICY_SCHEMA = "iii.documentation-policy/v1"
REVIEW_SCHEMA = "iii.documentation-review/v1"
REVIEW_CHECKS = (
    "current-architecture",
    "environment-boundary",
    "operator-safety",
    "standalone-links",
    "generated-reference-drift",
)
DOC_SUFFIXES = (".md", ".rst")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")
FENCED_BLOCK = re.compile(
    r"^```(?P<language>[^\n]*)\n(?P<body>.*?)^```\s*$", re.MULTILINE | re.DOTALL
)
MARKDOWN_HEADING = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")


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
        raise DocumentationError(
            f"unsupported documentation policy schema: {policy.get('schema')!r}"
        )
    repositories = policy.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise DocumentationError("documentation policy has no repositories")
    identifiers = [item.get("id") for item in repositories]
    paths = [item.get("path") for item in repositories]
    if len(identifiers) != len(set(identifiers)) or len(paths) != len(set(paths)):
        raise DocumentationError(
            "documentation repositories must have unique IDs and paths"
        )
    if any(
        not item.get("governed")
        or not item.get("entrypoint")
        or not isinstance(item.get("path"), str)
        for item in repositories
    ):
        raise DocumentationError(
            "every documentation repository needs a governed entrypoint"
        )
    editable = policy.get("editable_repository_roots")
    expected_editable = sorted(path for path in paths if path != ".")
    if editable != expected_editable:
        raise DocumentationError(
            "editable repository roots must exactly match governed non-workspace repositories"
        )
    generated = policy.get("generated_references")
    if not isinstance(generated, list) or not generated:
        raise DocumentationError("documentation policy has no generated references")
    generated_paths = [row.get("path") for row in generated]
    if len(generated_paths) != len(set(generated_paths)) or any(
        not row.get("generator") or not row.get("kind") for row in generated
    ):
        raise DocumentationError(
            "generated references require unique paths, kind, and generator"
        )
    authorities = policy.get("canonical_authorities", [])
    authority_ids = [row.get("id") for row in authorities]
    authority_paths = [row.get("path") for row in authorities]
    if (
        not authorities
        or len(authority_ids) != len(set(authority_ids))
        or len(authority_paths) != len(set(authority_paths))
        or any(
            not identifier or not path
            for identifier, path in zip(authority_ids, authority_paths)
        )
    ):
        raise DocumentationError(
            "canonical documentation authorities must be unique and complete"
        )
    if policy.get("authoring_contract") not in authority_paths:
        raise DocumentationError("authoring contract must be one canonical authority")
    for field in ("historical_index", "migration_review"):
        value = policy.get(field)
        if not isinstance(value, str) or not value:
            raise DocumentationError(f"documentation policy requires {field}")
    routers = policy.get("router_links")
    if not isinstance(routers, list) or not routers:
        raise DocumentationError("documentation policy has no router links")
    router_pairs = [
        (row.get("source"), row.get("target"))
        for row in routers
        if isinstance(row, dict)
    ]
    if len(router_pairs) != len(routers) or any(
        not source or not target or source == target for source, target in router_pairs
    ):
        raise DocumentationError(
            "router links require distinct source and target paths"
        )
    if len(router_pairs) != len(set(router_pairs)):
        raise DocumentationError("documentation router links must be unique")
    for pattern in policy.get("forbidden_current_patterns", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DocumentationError(
                f"invalid forbidden documentation pattern {pattern!r}: {exc}"
            ) from exc
    development_allowlist = policy.get("development_workspace_path_allowlist")
    if (
        not isinstance(development_allowlist, list)
        or any(
            not isinstance(value, str) or not value for value in development_allowlist
        )
        or len(development_allowlist) != len(set(development_allowlist))
    ):
        raise DocumentationError(
            "development workspace path allowlist must contain unique document keys"
        )
    return policy


def _git_files(repo: Path) -> tuple[str, ...]:
    process = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "*.md", "*.rst"],
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
    if (
        path.startswith("codex-backlogs/")
        or "backlog" in name
        or "plan" in name
        or name.endswith("-log.md")
    ):
        return ("historical-record", "engineering", False, False)
    if path.startswith("docs/") and (
        "operation" in name
        or "testing" in name
        or "maintenance" in name
        or "provision" in name
        or "recovery" in name
        or "verification" in name
    ):
        return ("runbook", "operator", True, True)
    if name == "readme.md":
        return ("canonical", "mixed", True, True)
    return ("contextual-design", "engineering", False, True)


def materialize_manifest(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    exclusions = policy.get("exclusions", [])
    generated = {row["path"]: row for row in policy.get("generated_references", [])}
    for repo_data in policy["repositories"]:
        repo = Repository(repo_data["id"], root / repo_data["path"])
        if not repo.path.is_dir():
            raise DocumentationError(
                f"governed repository is missing: {repo_data['path']}"
            )
        for path in _git_files(repo.path):
            is_excluded, reason = _excluded(
                path, exclusions if repo.id == "workspace" else ()
            )
            classification, audience, canonical, release_include = _classification(path)
            generated_row = generated.get(path) if repo.id == "workspace" else None
            if generated_row is not None:
                classification = "generated-reference"
                audience = "mixed"
                canonical = False
                release_include = True
            entries.append(
                {
                    "repository": repo.id,
                    "repository_path": repo_data["path"],
                    "path": path,
                    "owner": repo.id,
                    "context": (
                        repo.id if repo.id != "workspace" else "workspace-integration"
                    ),
                    "audience": audience,
                    "classification": "excluded" if is_excluded else classification,
                    "canonical": False if is_excluded else canonical,
                    "lifecycle": (
                        "excluded"
                        if is_excluded
                        else (
                            "maintained"
                            if classification != "historical-record"
                            else "historical"
                        )
                    ),
                    "source_of_truth": (
                        generated_row["generator"]
                        if generated_row is not None
                        else path
                    ),
                    "sha256": hashlib.sha256(
                        (repo.path / path).read_bytes()
                    ).hexdigest(),
                    "generated": generated_row is not None,
                    "qualified_release_inclusion": (
                        False if is_excluded else release_include
                    ),
                    "exclusion_reason": reason,
                }
            )
    entries.sort(key=lambda row: (row["repository"], row["path"]))
    body = {
        "schema": MANIFEST_SCHEMA,
        "policy_schema": policy["schema"],
        "canonical_roots": policy.get("canonical_roots", []),
        "documents": entries,
    }
    return {**body, "manifest_id": content_identity(body)}


def _slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.strip().lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"[ -]+", "-", value).strip("-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = MARKDOWN_HEADING.match(line)
        if not match:
            continue
        base = _slug(match.group("title"))
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _link_targets(file_path: Path) -> list[tuple[str, str | None]]:
    values: list[tuple[str, str | None]] = []
    text = file_path.read_text(encoding="utf-8")
    for match in MARKDOWN_LINK.finditer(text):
        raw = match.group("target").strip().split(maxsplit=1)[0].strip("<>")
        if raw.startswith(("http://", "https://", "mailto:", "app://")):
            continue
        target, separator, anchor = raw.partition("#")
        values.append((unquote(target), unquote(anchor) if separator else None))
    return values


def _local_link_errors(
    file_path: Path, logical_path: str, repository_root: Path
) -> list[str]:
    errors: list[str] = []
    repository_root = repository_root.resolve()
    for target, anchor in _link_targets(file_path):
        resolved = (file_path if not target else file_path.parent / target).resolve()
        try:
            resolved.relative_to(repository_root)
        except ValueError:
            errors.append(
                f"{logical_path}: local link escapes standalone repository {target!r}"
            )
            continue
        if not resolved.exists():
            errors.append(f"{logical_path}: broken local link {target!r}")
            continue
        if anchor and resolved.is_file() and resolved.suffix.lower() == ".md":
            if anchor not in _anchors(resolved):
                errors.append(f"{logical_path}: broken local anchor {target}#{anchor}")
    return errors


def materialize_review(manifest: Mapping[str, Any], *, reviewer: str) -> dict[str, Any]:
    """Bind an explicit migration approval to every maintained document byte."""

    if not reviewer.strip():
        raise DocumentationError("documentation migration reviewer must be non-empty")
    documents = [
        {
            "repository": row["repository"],
            "path": row["path"],
            "sha256": row["sha256"],
            "status": "passed",
            "checks": list(REVIEW_CHECKS),
        }
        for row in manifest.get("documents", [])
        if row.get("lifecycle") == "maintained"
    ]
    documents.sort(key=lambda row: (row["repository"], row["path"]))
    body = {
        "schema": REVIEW_SCHEMA,
        "manifest_id": manifest.get("manifest_id"),
        "reviewer": reviewer.strip(),
        "documents": documents,
    }
    return {**body, "review_id": content_identity(body)}


def read_review(path: Path) -> dict[str, Any]:
    value = read_manifest(path)
    if value.get("schema") != REVIEW_SCHEMA:
        raise DocumentationError(
            f"unsupported documentation review schema: {value.get('schema')!r}"
        )
    return value


def _review_errors(
    root: Path, policy: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
    path = root / policy["migration_review"]
    try:
        review = read_review(path)
    except DocumentationError as exc:
        return [str(exc)]
    body = {key: value for key, value in review.items() if key != "review_id"}
    if review.get("review_id") != content_identity(body):
        return ["documentation migration review identity mismatch"]
    errors: list[str] = []
    if review.get("manifest_id") != manifest.get("manifest_id"):
        errors.append("documentation migration review targets another manifest")
    expected = {
        (row["repository"], row["path"]): row["sha256"]
        for row in manifest.get("documents", [])
        if row.get("lifecycle") == "maintained"
    }
    observed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in review.get("documents", []):
        key = (row.get("repository"), row.get("path"))
        if key in observed:
            errors.append(f"duplicate documentation migration review {key[0]}:{key[1]}")
        observed[key] = row
    for key in sorted(expected.keys() - observed.keys()):
        errors.append(f"documentation migration review is missing {key[0]}:{key[1]}")
    for key in sorted(observed.keys() - expected.keys()):
        errors.append(
            f"documentation migration review has stale entry {key[0]}:{key[1]}"
        )
    for key in sorted(expected.keys() & observed.keys()):
        row = observed[key]
        if row.get("sha256") != expected[key]:
            errors.append(
                f"documentation migration review is stale for {key[0]}:{key[1]}"
            )
        if row.get("status") != "passed":
            errors.append(
                f"documentation migration review did not pass for {key[0]}:{key[1]}"
            )
        if row.get("checks") != list(REVIEW_CHECKS):
            errors.append(
                f"documentation migration review is incomplete for {key[0]}:{key[1]}"
            )
    return errors


def _historical_index_errors(
    root: Path, policy: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
    index_path = (root / policy["historical_index"]).resolve()
    if not index_path.is_file():
        return ["historical documentation index is missing"]
    indexed = {
        (index_path if not target else index_path.parent / target).resolve()
        for target, _anchor in _link_targets(index_path)
    }
    historical = {
        (root / row["repository_path"] / row["path"]).resolve(): (
            row["repository"],
            row["path"],
        )
        for row in manifest.get("documents", [])
        if row.get("lifecycle") == "historical"
    }
    return [
        f"historical documentation is not indexed: {key[0]}:{key[1]}"
        for path, key in sorted(historical.items(), key=lambda item: item[1])
        if path not in indexed
    ]


def _parser_inventory() -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    from iii.__main__ import build_parser
    from iii.runner import inventory_parser

    leaves = set(inventory_parser(build_parser()))
    prefixes = {path[:index] for path in leaves for index in range(1, len(path) + 1)}
    return leaves, prefixes


def _iii_commands(text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for block in FENCED_BLOCK.finditer(text):
        language = block.group("language").strip().lower()
        if language not in {"", "bash", "console", "sh", "shell", "zsh"}:
            continue
        logical: list[str] = []
        buffer = ""
        for raw in block.group("body").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            buffer = f"{buffer} {line}".strip()
            if buffer.endswith("\\"):
                buffer = buffer[:-1].rstrip()
                continue
            logical.append(buffer)
            buffer = ""
        if buffer:
            logical.append(buffer)
        for line in logical:
            match = re.search(
                r"(?:^|[;&|]\s*|\$\s+)(?P<command>(?:[^\s]+/)?iii)\s+", line
            )
            if not match:
                continue
            try:
                tokens = shlex.split(line[match.start("command") :])
            except ValueError:
                continue
            if tokens:
                commands.append(tokens)
    return commands


def _command_errors(file_path: Path, logical_path: str) -> list[str]:
    from iii.__main__ import build_parser

    leaves, prefixes = _parser_inventory()
    parser = build_parser()
    errors: list[str] = []
    universal_with_value = {"--output", "--operation-id"}
    universal_flags = {
        "--json",
        "--dry-run",
        "--plan",
        "--yes",
        "--no",
        "--confirm",
        "--interactive",
        "--non-interactive",
        "--resume",
    }
    for tokens in _iii_commands(file_path.read_text(encoding="utf-8")):
        values = []
        for token in tokens[1:]:
            if token in {";", "&&", "||", "|"}:
                break
            values.append(token)
        filtered: list[str] = []
        skip = False
        for token in values:
            if skip:
                skip = False
                continue
            if token in universal_with_value:
                skip = True
                continue
            if (
                token.startswith(("--output=", "--operation-id="))
                or token in universal_flags
            ):
                continue
            filtered.append(token)
        path: tuple[str, ...] = ()
        for token in filtered:
            candidate = (*path, token)
            if candidate in prefixes:
                path = candidate
                continue
            break
        if not path:
            errors.append(f"{logical_path}: unknown III command {' '.join(tokens[:3])}")
        elif path not in leaves and "--help" not in values:
            errors.append(
                f"{logical_path}: incomplete III command {' '.join(tokens[:len(path)+1])}"
            )
        elif path in leaves:
            selected = _selected_parser(parser, path)
            supported_options = {
                option
                for action in selected._actions
                for option in action.option_strings
            }
            supported_options.update(universal_with_value)
            supported_options.update(universal_flags)
            for token in values:
                if not token.startswith("-") or token == "-":
                    continue
                option = token.split("=", 1)[0]
                if option not in supported_options:
                    errors.append(
                        f"{logical_path}: unsupported option {option!r} for iii {' '.join(path)}"
                    )
    return errors


def _selected_parser(root: Any, path: tuple[str, ...]) -> Any:
    current = root
    for name in path:
        choices = {}
        for action in current._actions:
            if action.__class__.__name__ == "_SubParsersAction":
                choices.update(action.choices)
        current = choices[name]
    return current


def render_cli_reference() -> str:
    from iii.__main__ import build_parser
    from iii.runner import inventory_parser

    parser = build_parser()
    inventory = inventory_parser(parser)
    lines = [
        "# Generated III Command Reference",
        "",
        "Generated by `deployment/scripts/update_documentation_references.py`.",
        "Do not edit by hand. Universal structured output uses `--output=json`.",
        "",
    ]
    for path, spec in sorted(inventory.items()):
        selected = _selected_parser(parser, path)
        lines.extend(
            [
                f"## `iii {' '.join(path)}`",
                "",
                f"- Mutating: `{'yes' if spec.mutating else 'no'}`",
                f"- Interactive terminal: `{'yes' if spec.interactive else 'no'}`",
                "",
                "```text",
                selected.format_help().rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_schema_reference(root: Path) -> str:
    lines = [
        "# Generated Deployment Schema Reference",
        "",
        "Generated by `deployment/scripts/update_documentation_references.py`.",
        "Do not edit by hand. Schemas are Draft 7 contracts under `deployment/schemas/v1`.",
        "",
        "| Contract | Declared schema value | Required top-level fields |",
        "|---|---|---|",
    ]
    for path in sorted((root / "deployment/schemas/v1").glob("*.schema.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        schema_value = value.get("properties", {}).get("schema", {}).get("const")
        if schema_value is None:
            schema_value = (
                value.get("properties", {}).get("schema_version", {}).get("const", "-")
            )
        required = ", ".join(f"`{field}`" for field in value.get("required", [])) or "-"
        lines.append(f"| `{path.name}` | `{schema_value}` | {required} |")
    return "\n".join(lines).rstrip() + "\n"


def generated_references(root: Path, policy: Mapping[str, Any]) -> dict[Path, str]:
    renderers = {
        "cli-help": lambda: render_cli_reference(),
        "json-schema": lambda: render_schema_reference(root),
    }
    values: dict[Path, str] = {}
    for row in policy["generated_references"]:
        renderer = renderers.get(row["kind"])
        if renderer is None:
            raise DocumentationError(
                f"unsupported generated reference kind: {row['kind']}"
            )
        values[root / row["path"]] = renderer()
    return values


def _inventory_errors(
    root: Path, policy: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
    errors: list[str] = []
    lock_paths = {
        line.split()[0]
        for line in (root / policy["governed_lock"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    }
    editable = set(policy["editable_repository_roots"])
    missing_lock = sorted(editable - lock_paths)
    if missing_lock:
        errors.append(
            "editable repositories missing from submodule lock: "
            + ", ".join(missing_lock)
        )
    repository_rows = {row["id"]: row for row in policy["repositories"]}
    manifest_keys = {(row["repository"], row["path"]) for row in manifest["documents"]}
    for identifier, row in repository_rows.items():
        if (identifier, row["entrypoint"]) not in manifest_keys:
            errors.append(
                f"{identifier}: repository entrypoint is absent from manifest"
            )
    for row in policy["generated_references"]:
        if ("workspace", row["path"]) not in manifest_keys:
            errors.append(f"generated reference is absent from manifest: {row['path']}")
    for row in policy["canonical_authorities"]:
        if ("workspace", row["path"]) not in manifest_keys:
            errors.append(f"canonical authority is absent from manifest: {row['path']}")
    return errors


def _router_errors(root: Path, policy: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    graph: dict[str, set[str]] = {}
    for row in policy.get("router_links", []):
        source = row["source"]
        target = row["target"]
        source_path = root / source
        target_path = (root / target).resolve()
        if not source_path.is_file() or not target_path.exists():
            errors.append(f"router link is missing: {source} -> {target}")
            continue
        resolved_links = {
            (source_path.parent / link).resolve()
            for link, _anchor in _link_targets(source_path)
            if link
        }
        if target_path not in resolved_links:
            errors.append(f"router does not explicitly link: {source} -> {target}")
        graph.setdefault(source, set()).add(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            errors.append(f"documentation router cycle includes {node}")
            return
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, set()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    return errors


def audit_manifest(
    root: Path, policy: dict[str, Any], manifest: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return [
            f"unsupported documentation manifest schema: {manifest.get('schema')!r}"
        ]
    expected = materialize_manifest(root, policy)
    if manifest != expected:
        expected_keys = {
            (row["repository"], row["path"]) for row in expected["documents"]
        }
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
            errors.append(
                "documentation manifest metadata differs from policy-derived inventory"
            )
    errors.extend(_inventory_errors(root, policy, expected))
    errors.extend(_router_errors(root, policy))
    errors.extend(_review_errors(root, policy, expected))
    errors.extend(_historical_index_errors(root, policy, expected))
    for path, content in generated_references(root, policy).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            errors.append(
                f"generated documentation reference is stale: {path.relative_to(root)}"
            )
    maintained_by_key = {
        f"{row['repository']}:{row['path']}": row
        for row in manifest.get("documents", [])
        if row.get("lifecycle") == "maintained"
    }
    for allowed in policy.get("development_workspace_path_allowlist", []):
        row = maintained_by_key.get(allowed)
        if row is None:
            errors.append(f"development workspace path allowlist is stale: {allowed}")
            continue
        path = root / row["repository_path"] / row["path"]
        if "/home/iii/ws" not in path.read_text(encoding="utf-8"):
            errors.append(
                f"development workspace path allowlist is unnecessary: {allowed}"
            )
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
        if file_path.suffix.lower() == ".md":
            errors.extend(
                _local_link_errors(file_path, f"{key[0]}:{key[1]}", repository_root)
            )
            errors.extend(_command_errors(file_path, f"{key[0]}:{key[1]}"))
        content = file_path.read_text(encoding="utf-8")
        for term in policy.get("forbidden_current_terms", []):
            if term in content:
                errors.append(f"{key[0]}:{key[1]}: forbidden current term {term!r}")
        for pattern in policy.get("forbidden_current_patterns", []):
            if re.search(pattern, content):
                errors.append(
                    f"{key[0]}:{key[1]}: forbidden current pattern {pattern!r}"
                )
        if "/home/iii/ws" in content and f"{key[0]}:{key[1]}" not in policy.get(
            "development_workspace_path_allowlist", []
        ):
            errors.append(
                f"{key[0]}:{key[1]}: development workspace path is not explicitly allowed"
            )
        if entry["generated"]:
            configured = {
                row["path"]: row["generator"] for row in policy["generated_references"]
            }
            if entry["source_of_truth"] != configured.get(entry["path"]):
                errors.append(f"{key[0]}:{key[1]}: generated source-of-truth mismatch")
    return errors


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentationError(f"invalid documentation manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise DocumentationError("documentation manifest must contain one object")
    return value
