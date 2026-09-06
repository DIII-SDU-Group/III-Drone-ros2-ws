from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
QUALIFIED = ROOT / ".github/workflows/qualified-release.yml"
STATUS = ROOT / ".github/workflows/release-status.yml"


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_qualified_workflow_is_tag_only_strictly_revalidated_and_secret_separated() -> None:
    value = _load(QUALIFIED)
    text = QUALIFIED.read_text(encoding="utf-8")
    assert set(value["on"]) == {"push"}
    assert "pull_request" not in text
    assert '--event-ref "$GITHUB_REF"' in text
    assert "--release-ref refs/remotes/origin/release" in text
    assert value["permissions"] == {}
    assert value["jobs"]["sign"]["environment"] == "qualified-signing"
    assert value["jobs"]["sign"]["permissions"] == {"contents": "read"}
    assert "III_QUALIFIED_SIGNING_KEY_PEM" in text
    assert "III_QUALIFIED_SIGNING_KEY_PEM" not in text[text.index("  publish:"):]
    assert value["jobs"]["publish"]["permissions"] == {"contents": "write"}
    assert value["jobs"]["initial-status"]["environment"] == "release-status"
    assert "pip install --disable-pip-version-check ./src/III-Drone-Contracts" in text
    failure_steps = value["jobs"]["retain-failure"]["steps"]
    failure_checkout = next(step for step in failure_steps if "uses" in step)
    assert failure_checkout["with"]["ref"] == "refs/heads/release"


def test_qualified_workflow_retains_exact_complete_check_matrix_and_failure_attempt() -> None:
    text = QUALIFIED.read_text(encoding="utf-8")
    for check in (
        "arm64-build", "arm64-tests", "dependency-lock", "deployment-contracts",
        "gc-build", "gc-tests", "governance-audit", "promotion-evidence", "px4-build",
    ):
        assert f"--check {check}=" in text
    assert "--qualified-paired" in text
    assert "run_target_abi_probe.py" in text
    assert "publish-failure" in text
    assert "iii-attempt" not in text  # naming is owned by validated publisher code
    assert "needs.initial-status.result == 'failure'" in text


def test_all_third_party_actions_are_commit_pinned() -> None:
    for path in (QUALIFIED, STATUS):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses:") and not stripped.startswith("- uses:"):
                continue
            reference = stripped.split("uses:", 1)[1].strip()
            assert "@" in reference
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40 and all(character in "0123456789abcdef" for character in revision)


def test_status_workflow_is_serialized_release_branch_only_and_has_no_user_code_surface() -> None:
    value = _load(STATUS)
    text = STATUS.read_text(encoding="utf-8")
    job = value["jobs"]["publish-status"]
    assert set(value["on"]) == {"workflow_dispatch"}
    assert value["concurrency"]["group"] == "iii-release-status"
    assert job["if"] == "github.ref == 'refs/heads/release'"
    assert job["environment"] == "release-status"
    assert job["permissions"] == {"contents": "write"}
    assert "ref: refs/heads/release" in text
    assert "expected_statement_id" in value["on"]["workflow_dispatch"]["inputs"]
    assert "client_operation_id" in value["on"]["workflow_dispatch"]["inputs"]
    assert "--operation-id \"$CLIENT_OPERATION_ID\"" in text
    assert "python scripts/release/publish_release_status.py" in text
    assert "eval " not in text and "bash -c" not in text
