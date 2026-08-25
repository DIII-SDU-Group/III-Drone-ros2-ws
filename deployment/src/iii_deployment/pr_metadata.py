"""Untrusted pull-request marker parsing bound to trusted repository facts."""

from __future__ import annotations

from dataclasses import dataclass
import configparser
from io import StringIO
import re
from typing import Mapping, Sequence
from urllib.parse import urlparse

from .contracts import ContractError


MARKER = re.compile(
    r"<!--\s*iii-submodule-pr:\s*path=([^\s]+)\s+"
    r"url=(https://github\.com/([^/\s]+/[^/\s]+)/pull/([0-9]+))\s*-->"
)
III_PATH = re.compile(r"^(?:src|tools)/III-Drone-[A-Za-z-]+$")


@dataclass(frozen=True)
class PullRequestClaim:
    path: str
    url: str
    repository: str
    number: int


def _repository_from_url(url: str) -> str:
    if url.startswith("git@github.com:"):
        value = url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(url)
        if parsed.hostname != "github.com":
            raise ContractError(f"submodule URL is not a GitHub repository: {url}")
        value = parsed.path.lstrip("/")
    return value.removesuffix(".git")


def trusted_submodule_repositories(gitmodules: str) -> dict[str, str]:
    parser = configparser.ConfigParser()
    try:
        parser.read_file(StringIO(gitmodules))
    except configparser.Error as exc:
        raise ContractError(f"trusted .gitmodules is invalid: {exc}") from exc
    result: dict[str, str] = {}
    for section in parser.sections():
        if not section.startswith('submodule "'):
            continue
        path = parser.get(section, "path", fallback="")
        url = parser.get(section, "url", fallback="")
        if III_PATH.fullmatch(path):
            result[path] = _repository_from_url(url)
    return result


def validate_pr_marker_claims(
    body: str,
    *,
    changed_paths: Sequence[str],
    trusted_repositories: Mapping[str, str],
) -> tuple[PullRequestClaim, ...]:
    changed = {path for path in changed_paths if III_PATH.fullmatch(path)}
    claims: dict[str, PullRequestClaim] = {}
    for match in MARKER.finditer(body):
        path, url, repository, number = match.groups()
        if not III_PATH.fullmatch(path):
            raise ContractError(f"untrusted PR marker has invalid III path {path!r}")
        if path in claims:
            raise ContractError(f"untrusted PR metadata contains duplicate marker for {path}")
        expected_repository = trusted_repositories.get(path)
        if expected_repository is None:
            raise ContractError(f"untrusted PR marker names undeclared submodule {path}")
        if repository != expected_repository:
            raise ContractError(
                f"untrusted PR marker repository mismatch for {path}: "
                f"expected {expected_repository}, observed {repository}"
            )
        claims[path] = PullRequestClaim(path, url, repository, int(number))
    if set(claims) != changed:
        missing = sorted(changed - set(claims))
        unexpected = sorted(set(claims) - changed)
        detail = []
        if missing:
            detail.append("missing markers: " + ", ".join(missing))
        if unexpected:
            detail.append("markers for unchanged paths: " + ", ".join(unexpected))
        raise ContractError("untrusted PR marker set does not match changed III gitlinks: " + "; ".join(detail))
    return tuple(claims[path] for path in sorted(claims))
