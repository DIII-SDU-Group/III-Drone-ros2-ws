"""Branch promotion, impact, signed evidence, and waiver policy."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity


BRANCH_SCHEMA = "iii.branch-policy/v1"
IMPACT_SCHEMA = "iii.change-impact-policy/v1"


def load_json(path: Path, expected_schema: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if value.get("schema") != expected_schema:
        raise ContractError(f"unsupported policy schema in {path}: {value.get('schema')!r}")
    return value


def classify_head(head: str, protected: Iterable[str]) -> str:
    if head in protected:
        return head
    if re.fullmatch(r"promote/develop-to-main/[a-z0-9][a-z0-9._-]*", head):
        return "promote/develop-to-main/*"
    return "feature"


def validate_pr_source(policy: Mapping[str, Any], *, repository_kind: str, base: str, head: str) -> None:
    if repository_kind not in {"workspace", "submodule"}:
        raise ContractError(f"unknown repository kind {repository_kind!r}")
    rules = policy[repository_kind]
    if base not in rules:
        raise ContractError(f"{repository_kind} PR target {base!r} is not governed/allowed")
    head_class = classify_head(head, policy["protected_branches"])
    if head_class not in rules[base]["sources"]:
        raise ContractError(f"source/base rejected: {head} ({head_class}) -> {base}")
    if head == base:
        raise ContractError("pull request source and base cannot be identical")


def validate_mechanical_diff(policy: Mapping[str, Any], paths: Iterable[str]) -> None:
    unexpected = [
        path for path in paths
        if not any(fnmatch.fnmatch(path, pattern) for pattern in policy["mechanical_workspace_paths"])
    ]
    if unexpected:
        raise ContractError("mechanical promotion contains non-gitlink/lock paths: " + ", ".join(sorted(unexpected)))


def governed_source_identity(root: Path) -> str:
    """Hash tracked workspace blobs plus III submodule trees, not commit identities.

    The dependency lock is deliberately excluded because a mechanical promotion
    replaces develop commits with content-equivalent main merge commits and then
    refreshes those literal commit IDs. Lock validity is checked independently.
    """

    process = subprocess.run(
        ["git", "ls-files", "-s"], cwd=root, capture_output=True, text=True, check=False
    )
    if process.returncode:
        raise ContractError(process.stderr.strip() or "cannot inventory governed source")
    entries: list[tuple[str, str]] = []
    for line in process.stdout.splitlines():
        metadata, path = line.split("\t", 1)
        mode, object_id, _stage = metadata.split()
        if path == "deps/submodule-lock.txt":
            continue
        if mode == "160000" and (path.startswith("src/III-") or path.startswith("tools/III-")):
            tree = subprocess.run(
                ["git", "-C", str(root / path), "rev-parse", "HEAD^{tree}"],
                capture_output=True, text=True, check=False,
            )
            if tree.returncode:
                raise ContractError(f"cannot resolve III submodule tree for {path}")
            object_id = f"tree:{tree.stdout.strip()}"
        entries.append((path, f"{mode}:{object_id}"))
    return hashlib.sha256(canonical_json(entries)).hexdigest()


def required_evidence(impact_policy: Mapping[str, Any], paths: Iterable[str]) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    path_list = tuple(paths)
    for rule in impact_policy["rules"]:
        matches = sorted({path for path in path_list if any(fnmatch.fnmatch(path, pattern) for pattern in rule["patterns"])})
        if matches:
            for category in rule["categories"]:
                reasons.setdefault(category, []).append(f"{rule['id']}: {', '.join(matches)}")
    if not reasons:
        for category in impact_policy["default_categories"]:
            reasons.setdefault(category, []).append("DEFAULT: unclassified governed change")
    return reasons


def validate_waivers(
    impact_policy: Mapping[str, Any], required: Iterable[str], categories: Mapping[str, str], waivers: Iterable[Mapping[str, Any]]
) -> None:
    waiver_by_category = {waiver["category"]: waiver for waiver in waivers}
    for category in required:
        status = categories.get(category)
        if status == "passed":
            continue
        if status != "not-performed":
            raise ContractError(f"required evidence category {category} is missing or failed")
        if category in impact_policy["non_waivable"]:
            raise ContractError(f"non-waivable evidence category {category} was not performed")
        if category not in impact_policy["physical_waivable"] or category not in waiver_by_category:
            raise ContractError(f"missing signed waiver for unavailable category {category}")
        waiver = waiver_by_category[category]
        if not all(waiver.get(field) for field in ("rationale", "risk", "compensating_evidence")):
            raise ContractError(f"incomplete waiver for {category}")


def _decode_base64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise ContractError("invalid base64url signature") from exc


def verify_attestation(
    attestation: Mapping[str, Any], *, registry: ContractRegistry, trusted_signers: Mapping[str, str]
) -> None:
    registry.validate("promotion-evidence", attestation)
    signer_id = attestation["signer_id"]
    encoded_key = trusted_signers.get(signer_id)
    if encoded_key is None:
        raise ContractError(f"untrusted promotion signer {signer_id}")
    unsigned = dict(attestation)
    signature = _decode_base64url(unsigned.pop("signature"))
    identity_payload = dict(unsigned)
    identity_payload.pop("attestation_id")
    if content_identity(identity_payload) != attestation["attestation_id"]:
        raise ContractError("attestation content identity mismatch")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_decode_base64url(encoded_key))
        public_key.verify(signature, canonical_json(unsigned))
    except (ValueError, InvalidSignature) as exc:
        raise ContractError("invalid promotion evidence signature") from exc


def validate_attestation_binding(
    attestation: Mapping[str, Any], *, source_identity: str, impact_policy: Mapping[str, Any]
) -> None:
    if attestation["source_content_identity"] != source_identity:
        raise ContractError("promotion evidence source-content identity does not match candidate")
    if attestation["policy_sha256"] != content_identity(impact_policy):
        raise ContractError("promotion evidence policy identity is stale")
