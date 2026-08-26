"""Fail-closed qualified-tag preflight and recovery-anchor ownership."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from .contracts import ContractError, ContractRegistry, SEMVER, classify_release


REQUIRED_QUALIFICATION_CHECKS = frozenset(
    {
        "arm64-build",
        "arm64-tests",
        "dependency-lock",
        "deployment-contracts",
        "gc-build",
        "gc-tests",
        "governance-audit",
        "promotion-evidence",
    }
)


@dataclass(frozen=True)
class QualificationCheck:
    id: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class QualificationReport:
    mode: str
    version: str
    source_commit: str | None
    release_commit: str | None
    checks: tuple[QualificationCheck, ...]

    @property
    def verified(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "iii.qualification-preflight-result/v1",
            "mode": self.mode,
            "version": self.version,
            "source_commit": self.source_commit,
            "release_commit": self.release_commit,
            "verified": self.verified,
            "checks": [check.to_dict() for check in self.checks],
        }

    def require_verified(self) -> "QualificationReport":
        if not self.verified:
            failures = "; ".join(
                f"{check.id}: {check.detail}" for check in self.checks if not check.passed
            )
            raise ContractError(f"qualified release preflight refused: {failures}")
        return self


def _run(root: Path, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run(root, ("git", *arguments))


def _resolve(root: Path, revision: str) -> str | None:
    process = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return process.stdout.strip() if process.returncode == 0 else None


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_evidence(path: Path, registry: ContractRegistry) -> tuple[Mapping[str, Any] | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ContractError("qualification evidence must be a JSON object")
        registry.validate("qualification-evidence", value)
        return value, "schema-valid and complete"
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        return None, str(exc)


def inspect_qualification(
    root: Path,
    *,
    version: str,
    evidence_path: Path,
    mode: str,
    release_ref: str = "refs/remotes/origin/release",
    lock_path: Path | None = None,
    lock_command: Sequence[str] = ("scripts/git/verify_submodule_lock.sh",),
    registry: ContractRegistry | None = None,
    require_evidence: bool = True,
) -> QualificationReport:
    """Inspect publication or tag-build state without mutating the repository.

    ``publish`` requires HEAD to be the exact release head and the version to be
    unused. ``build`` requires the existing tag to resolve to HEAD. Both modes
    require a clean recursive checkout, a verified lock, and complete evidence
    bound to the candidate commit and version.
    """

    if mode not in {"publish", "build"}:
        raise ContractError(f"unknown qualification preflight mode {mode!r}")
    root = root.resolve()
    registry = registry or ContractRegistry(root / "deployment" / "schemas" / "v1")
    lock_path = lock_path or root / "deps" / "submodule-lock.txt"
    checks: list[QualificationCheck] = []

    def add(identifier: str, passed: bool, detail: str) -> None:
        checks.append(QualificationCheck(identifier, passed, detail))

    strict_version = bool(SEMVER.fullmatch(version))
    add(
        "version.strict-semver",
        strict_version,
        "strict vMAJOR.MINOR.PATCH" if strict_version else "version is not strict SemVer",
    )
    source_commit = _resolve(root, "HEAD")
    release_commit = _resolve(root, release_ref)
    add("source.commit", source_commit is not None, source_commit or "HEAD is not a commit")
    add("release.ref", release_commit is not None, release_commit or f"cannot resolve {release_ref}")
    exact_release = source_commit is not None and release_commit is not None and source_commit == release_commit
    reachable = source_commit is not None and release_commit is not None and _is_ancestor(root, source_commit, release_commit)
    if mode == "publish":
        add("release.exact-head", exact_release, "HEAD equals release head" if exact_release else "HEAD is not the exact release head")
    else:
        add("release.reachable", reachable, "tag commit belongs to release" if reachable else "tag commit is outside release")

    tag_commit = _resolve(root, f"refs/tags/{version}")
    if mode == "publish":
        add("tag.absent-local", tag_commit is None, "tag is unused locally" if tag_commit is None else f"tag already resolves to {tag_commit}")
    else:
        add("tag.exact-commit", tag_commit is not None and tag_commit == source_commit, "tag resolves to HEAD" if tag_commit == source_commit and tag_commit is not None else "tag is missing or moved away from HEAD")

    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
    clean = status.returncode == 0 and not status.stdout.strip()
    add("source.clean", clean, "workspace and submodule gitlinks are clean" if clean else (status.stdout.strip() or status.stderr.strip() or "git status failed"))

    submodule_status = _git(root, "submodule", "foreach", "--quiet", "--recursive", "git status --porcelain=v1 --untracked-files=all")
    submodules_clean = submodule_status.returncode == 0 and not submodule_status.stdout.strip()
    add("submodules.clean", submodules_clean, "recursive submodule worktrees are clean" if submodules_clean else (submodule_status.stdout.strip() or submodule_status.stderr.strip() or "submodule status failed"))

    lock_process = _run(root, lock_command)
    add("dependency-lock.verified", lock_process.returncode == 0, "dependency lock verifier passed" if lock_process.returncode == 0 else (lock_process.stderr.strip() or lock_process.stdout.strip() or "dependency lock verifier failed"))
    lock_sha256 = _sha256(lock_path)
    add("dependency-lock.present", lock_sha256 is not None, lock_sha256 or f"cannot hash {lock_path}")

    evidence: Mapping[str, Any] | None = None
    if require_evidence:
        evidence, evidence_detail = _load_evidence(evidence_path, registry)
        add("evidence.contract", evidence is not None, evidence_detail)
    else:
        add("evidence.deferred", True, "qualification evidence is assembled after isolated checks")
    if require_evidence and evidence is not None:
        add("evidence.commit-binding", evidence["source_commit"] == source_commit, "evidence identifies HEAD" if evidence["source_commit"] == source_commit else "evidence source commit differs from HEAD")
        add("evidence.version-binding", evidence["version"] == version, "evidence identifies version" if evidence["version"] == version else "evidence version differs")
        add("evidence.lock-binding", evidence["dependency_lock_sha256"] == lock_sha256, "evidence identifies dependency lock" if evidence["dependency_lock_sha256"] == lock_sha256 else "evidence dependency-lock identity differs")
        unique_ids = {entry["id"] for entry in evidence["required_checks"]}
        add("evidence.unique-checks", len(unique_ids) == len(evidence["required_checks"]), "required check IDs are unique" if len(unique_ids) == len(evidence["required_checks"]) else "duplicate required check IDs")
        missing_checks = sorted(REQUIRED_QUALIFICATION_CHECKS - unique_ids)
        add(
            "evidence.required-check-set",
            not missing_checks,
            "all qualification check categories are retained"
            if not missing_checks
            else "missing required checks: " + ", ".join(missing_checks),
        )

    return QualificationReport(mode, version, source_commit, release_commit, tuple(checks))


def select_recovery_anchor(
    current_anchor: str | None,
    *,
    manifest: Mapping[str, Any],
    deployment: Mapping[str, Any],
) -> str | None:
    """Return the protected recovery anchor after a deployment result.

    Only an accepted activation of a valid qualified manifest can replace the
    anchor. Field-development and every non-accepted terminal result preserve it.
    Malformed attempts to claim qualification fail closed.
    """

    release_id = manifest.get("release_id")
    if manifest.get("release_class") != "qualified":
        return current_anchor
    preflight = deployment.get("qualification_preflight")
    accepted = (
        deployment.get("action") == "activate"
        and deployment.get("outcome") == "accepted"
        and deployment.get("release_id") == release_id
        and deployment.get("activated_release_id") == release_id
        and isinstance(preflight, Mapping)
        and preflight.get("verified") is True
    )
    if not accepted:
        return current_anchor
    classify_release(manifest, requested="qualified", preflight=preflight)
    return release_id
