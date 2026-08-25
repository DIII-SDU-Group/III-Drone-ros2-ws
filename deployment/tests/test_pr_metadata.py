from __future__ import annotations

import pytest

from iii_deployment.contracts import ContractError
from iii_deployment.pr_metadata import trusted_submodule_repositories, validate_pr_marker_claims


GITMODULES = """
[submodule "src/III-Drone-Core"]
    path = src/III-Drone-Core
    url = git@github.com:DIII-SDU-Group/III-Drone-Core.git
[submodule "src/third-party"]
    path = src/third-party
    url = https://github.com/example/third-party.git
"""


def _marker(path: str, repository: str, number: int = 7) -> str:
    return (
        f"<!-- iii-submodule-pr: path={path} "
        f"url=https://github.com/{repository}/pull/{number} -->"
    )


def test_marker_is_only_accepted_when_bound_to_changed_trusted_gitlink() -> None:
    repositories = trusted_submodule_repositories(GITMODULES)
    claims = validate_pr_marker_claims(
        _marker("src/III-Drone-Core", "DIII-SDU-Group/III-Drone-Core"),
        changed_paths=["README.md", "src/III-Drone-Core"],
        trusted_repositories=repositories,
    )
    assert len(claims) == 1
    assert claims[0].repository == "DIII-SDU-Group/III-Drone-Core"


def test_untrusted_marker_cannot_substitute_unrelated_merged_repository() -> None:
    with pytest.raises(ContractError, match="repository mismatch"):
        validate_pr_marker_claims(
            _marker("src/III-Drone-Core", "attacker/unrelated"),
            changed_paths=["src/III-Drone-Core"],
            trusted_repositories=trusted_submodule_repositories(GITMODULES),
        )


def test_marker_set_must_exactly_match_changed_iii_gitlinks() -> None:
    repositories = trusted_submodule_repositories(GITMODULES)
    with pytest.raises(ContractError, match="missing markers"):
        validate_pr_marker_claims("", changed_paths=["src/III-Drone-Core"], trusted_repositories=repositories)
    with pytest.raises(ContractError, match="unchanged paths"):
        validate_pr_marker_claims(
            _marker("src/III-Drone-Core", "DIII-SDU-Group/III-Drone-Core"),
            changed_paths=["README.md"],
            trusted_repositories=repositories,
        )


def test_duplicate_and_undeclared_markers_are_rejected() -> None:
    repositories = trusted_submodule_repositories(GITMODULES)
    marker = _marker("src/III-Drone-Core", "DIII-SDU-Group/III-Drone-Core")
    with pytest.raises(ContractError, match="duplicate"):
        validate_pr_marker_claims(marker + marker, changed_paths=["src/III-Drone-Core"], trusted_repositories=repositories)
    with pytest.raises(ContractError, match="undeclared"):
        validate_pr_marker_claims(
            _marker("src/III-Drone-Fake", "DIII-SDU-Group/III-Drone-Fake"),
            changed_paths=["src/III-Drone-Fake"],
            trusted_repositories=repositories,
        )
