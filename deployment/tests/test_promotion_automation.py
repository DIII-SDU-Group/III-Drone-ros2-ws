from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/git"


def _fake_adapters(tmp_path: Path) -> tuple[dict[str, str], Path]:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    log = tmp_path / "adapter.log"
    git = adapter_dir / "git"
    git.write_text(
        "#!/bin/sh\n"
        "printf 'git %s\\n' \"$*\" >> \"$ADAPTER_LOG\"\n"
        "if [ \"$1 $2\" = 'rev-parse --show-toplevel' ]; then printf '%s\\n' \"$FAKE_ROOT\"; exit 0; fi\n"
        "if [ \"$1 $2 $3\" = 'symbolic-ref --quiet --short' ]; then printf '%s\\n' develop; exit 0; fi\n"
        "exit 97\n",
        encoding="utf-8",
    )
    gh = adapter_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "printf 'gh %s\\n' \"$*\" >> \"$ADAPTER_LOG\"\n"
        "exit 98\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{adapter_dir}:{env['PATH']}",
        "ADAPTER_LOG": str(log),
        "FAKE_ROOT": str(ROOT),
    })
    return env, log


def _run(script: str, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_promotion_shell_scripts_parse() -> None:
    for script in (
        "create_stack_prs.sh", "push_stack.sh", "refresh_workspace_submodule_pointers.sh",
        "create_develop_to_main_prs.sh", "create_main_to_release_pr.sh",
    ):
        subprocess.run(["bash", "-n", str(SCRIPTS / script)], check=True)


def test_develop_to_main_prepare_is_dry_run_with_fixed_namespace(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_develop_to_main_prs.sh", "--promotion-id", "qual-2026-08", env=env,
    )
    assert process.returncode == 0, process.stderr
    assert "promote/develop-to-main/qual-2026-08" in process.stdout
    assert "create_stack_prs.sh --base main" in process.stdout
    assert "DRY-RUN complete" in process.stdout
    calls = log.read_text(encoding="utf-8")
    assert calls.count("\n") == 1
    assert "gh " not in calls


def test_develop_to_main_refresh_plan_is_idempotent_and_audited(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_develop_to_main_prs.sh", "--promotion-id", "qual-2026-08",
        "--phase", "refresh", env=env,
    )
    assert process.returncode == 0, process.stderr
    assert "refresh_workspace_submodule_pointers.sh --base main" in process.stdout
    assert "verify_submodule_lock.sh" in process.stdout
    assert "verify_promotion_source.py" in process.stdout
    assert "git commit" in process.stdout and "git push" in process.stdout
    assert "gh " not in log.read_text(encoding="utf-8")


def test_main_to_release_is_workspace_only_and_dry_run(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run("create_main_to_release_pr.sh", env=env)
    assert process.returncode == 0, process.stderr
    assert "Source branch: main" in process.stdout
    assert "Target branch: release" in process.stdout
    assert "Submodule release branches: prohibited" in process.stdout
    assert "gh " not in log.read_text(encoding="utf-8")


def test_main_stack_rejects_non_promotion_branch_before_github(tmp_path: Path) -> None:
    env, log = _fake_adapters(tmp_path)
    process = _run(
        "create_stack_prs.sh", "--base", "main", "--feature", "feature/wrong", env=env,
    )
    assert process.returncode != 0
    assert "promote/develop-to-main/<id>" in process.stderr
    assert "gh " not in log.read_text(encoding="utf-8")


def test_pointer_refresh_rejects_ambiguous_main_branch(tmp_path: Path) -> None:
    env, _log = _fake_adapters(tmp_path)
    process = _run(
        "refresh_workspace_submodule_pointers.sh", "--base", "main",
        "--feature", "release/develop-to-main-old", env=env,
    )
    assert process.returncode != 0
    assert "promote/develop-to-main/<id>" in process.stderr


def test_legacy_promotion_namespace_and_staging_branch_are_absent() -> None:
    governed = [
        ROOT / "README.md", ROOT / "docs/dependency-governance.md",
        ROOT / "docs/repo-boundary-map.md", SCRIPTS / "create_develop_to_main_prs.sh",
        SCRIPTS / "create_stack_prs.sh", SCRIPTS / "push_stack.sh",
        ROOT / ".github/workflows/refresh-submodule-pointers.yml",
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in governed)
    assert "release/develop-to-main" not in content
    assert "Workspace `staging`" not in content
